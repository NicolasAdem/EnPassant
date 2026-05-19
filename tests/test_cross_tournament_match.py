"""
Regression test for Codex audit finding B4: the report and confirm routes
must verify the {tid} in the path matches the match's tournament_id before
mutating state or broadcasting.

Pre-fix, app/routers/api.py:155 (report) and api.py:167 (confirm) looked the
match up by match_id only via the service layer. Posting a valid match_id
under the WRONG tournament's tid would:
  1. Mutate the real match in its actual tournament (T_right).
  2. Broadcast a state refresh to T_wrong's subscribers — desyncing them.
  3. Attribute the action to T_wrong in the audit log (events table writes
     keyed on m['tournament_id'] from the row, so the log actually lands on
     T_right — but the broadcast disagreement is the user-visible damage).

Fix: a router-level _require_match_in_tournament(tid, mid) guard that runs
before svc.report_result / svc.confirm_result and rejects with 404 when the
match either doesn't exist or belongs to a different tournament.

Tests:
    1. POST /report under wrong tid returns 404 (the headline regression).
    2. The 404 does NOT mutate the real match row.
    3. POST /confirm under wrong tid returns 404.
    4. Symmetry: posting under a tid that doesn't exist at all also 404s.
    5. Sanity: the same call under the CORRECT tid still works — guards
       against the fix accidentally closing the legitimate path. Same
       pattern as test_bye_no_table.py's "real-match table set still works"
       case.
    6. Broadcast isolation: T_wrong's state snapshot is unchanged after the
       rejected request. This is the property that actually matters for the
       audit finding — clients connected to T_wrong must not see ghost
       activity from a malformed cross-tournament request.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def client():
    """Fresh in-process app with a temp SQLite DB. Mirrors test_dispute_resolve.py."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name

    from app import database
    database.DB_PATH = db_path
    database.init_db()

    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c

    try:
        os.unlink(db_path)
    except OSError:
        pass


# ---------- Helpers ----------

def _make_tournament_with_pending_match(client, name):
    """Create a tournament, add 2 players, start round 1, return
    (tid, host_token, match_id, white_pid, black_pid). The round-1 match is
    in 'pending' status — no result reported yet."""
    r = client.post("/api/tournaments", json={"name": name})
    assert r.status_code == 200, r.text
    body = r.json()
    tid, host_token = body["id"], body["host_token"]

    r = client.post(f"/api/tournaments/{tid}/players", json={"name": "Alice"})
    assert r.status_code == 200
    r = client.post(f"/api/tournaments/{tid}/players", json={"name": "Bob"})
    assert r.status_code == 200

    r = client.post(
        f"/api/tournaments/{tid}/rounds",
        params={"host_token": host_token},
        json={},
    )
    assert r.status_code == 200, r.text
    pairing = r.json()["pairings"][0]
    white_pid = pairing["white_player_id"]
    black_pid = pairing["black_player_id"]
    assert black_pid is not None

    r = client.get(f"/api/tournaments/{tid}/state")
    assert r.status_code == 200
    match_id = r.json()["current_matches"][0]["id"]

    return tid, host_token, match_id, white_pid, black_pid


def _read_match_row(match_id):
    """Read the raw match row from SQLite without filtering by tournament_id.
    Used to confirm a rejected request did not mutate the real row.

    Unlike test_dispute_resolve.py's helper, this one deliberately does NOT
    take a tid — the whole point of B4 is that the row exists in exactly one
    tournament, and we want to read it regardless of which tid we tried to
    abuse it from."""
    from app.database import db
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM matches WHERE id = ?", (match_id,)
        ).fetchone()
        assert row is not None, f"match row {match_id} missing"
        return dict(row)


# ---------- Headline regression ----------

def test_report_under_wrong_tid_is_rejected(client):
    """The bug: POST /api/tournaments/{T_wrong}/matches/{mid}/report with a
    real match_id from T_right mutated T_right's match and broadcast to
    T_wrong. Fix: router-level guard returns 404 first."""
    tid_a, _ht_a, match_id_a, white_pid_a, _black_pid_a = \
        _make_tournament_with_pending_match(client, "Tournament A")
    tid_b, _ht_b, _match_id_b, _white_pid_b, _black_pid_b = \
        _make_tournament_with_pending_match(client, "Tournament B")

    # Hit A's match via B's tid. Pre-fix: 200 + match in A flips to 'reported'
    # + B's subscribers see a state event. Post-fix: 404, no mutation.
    r = client.post(
        f"/api/tournaments/{tid_b}/matches/{match_id_a}/report",
        json={"player_id": white_pid_a, "result": "white"},
    )
    assert r.status_code == 404, (
        f"expected 404 on cross-tournament report, got {r.status_code}: {r.text}"
    )


def test_report_under_wrong_tid_does_not_mutate(client):
    """The 404 is half the story — the real match row must be unchanged."""
    tid_a, _ht_a, match_id_a, white_pid_a, _black_pid_a = \
        _make_tournament_with_pending_match(client, "Tournament A")
    tid_b, _ht_b, _match_id_b, _white_pid_b, _black_pid_b = \
        _make_tournament_with_pending_match(client, "Tournament B")

    before = _read_match_row(match_id_a)
    assert before["status"] == "pending"
    assert before["result"] is None
    assert before["reported_by"] is None

    r = client.post(
        f"/api/tournaments/{tid_b}/matches/{match_id_a}/report",
        json={"player_id": white_pid_a, "result": "white"},
    )
    assert r.status_code == 404

    after = _read_match_row(match_id_a)
    assert after["status"] == "pending", "status changed despite 404"
    assert after["result"] is None, "result mutated despite 404"
    assert after["reported_by"] is None, "reported_by mutated despite 404"


def test_confirm_under_wrong_tid_is_rejected(client):
    """Same regression on the confirm endpoint. Set up a reported match in A,
    then try to confirm it via B's tid."""
    tid_a, _ht_a, match_id_a, white_pid_a, black_pid_a = \
        _make_tournament_with_pending_match(client, "Tournament A")
    tid_b, _ht_b, _match_id_b, _white_pid_b, _black_pid_b = \
        _make_tournament_with_pending_match(client, "Tournament B")

    # Legitimately report on A first
    r = client.post(
        f"/api/tournaments/{tid_a}/matches/{match_id_a}/report",
        json={"player_id": white_pid_a, "result": "white"},
    )
    assert r.status_code == 200

    before = _read_match_row(match_id_a)
    assert before["status"] == "reported"

    # Cross-tournament confirm attempt
    r = client.post(
        f"/api/tournaments/{tid_b}/matches/{match_id_a}/confirm",
        json={"player_id": black_pid_a, "agree": True},
    )
    assert r.status_code == 404

    after = _read_match_row(match_id_a)
    assert after["status"] == "reported", "confirm leaked through wrong tid"
    assert after["confirmed_by"] is None


def test_report_under_nonexistent_tid_is_rejected(client):
    """A tid that doesn't correspond to any tournament should also 404 (not
    500). Pre-fix this would have hit the same code path as the cross-
    tournament case; the guard collapses both into the same response."""
    tid_a, _ht_a, match_id_a, white_pid_a, _black_pid_a = \
        _make_tournament_with_pending_match(client, "Tournament A")

    r = client.post(
        f"/api/tournaments/NOPE99/matches/{match_id_a}/report",
        json={"player_id": white_pid_a, "result": "white"},
    )
    assert r.status_code == 404

    assert _read_match_row(match_id_a)["status"] == "pending"


# ---------- Sanity: legitimate path still works ----------

def test_report_under_correct_tid_still_works(client):
    """Guards against the fix accidentally closing the legitimate path —
    same pattern as the 'real-match table set still works' case in B1's
    test_bye_no_table.py."""
    tid_a, _ht_a, match_id_a, white_pid_a, _black_pid_a = \
        _make_tournament_with_pending_match(client, "Tournament A")

    r = client.post(
        f"/api/tournaments/{tid_a}/matches/{match_id_a}/report",
        json={"player_id": white_pid_a, "result": "white"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reported"

    assert _read_match_row(match_id_a)["status"] == "reported"


# ---------- Broadcast isolation ----------

def test_wrong_tournament_state_unchanged_after_rejected_report(client):
    """The user-visible damage of B4 was that T_wrong's subscribers received
    a state event for an action they had nothing to do with. Verify by
    snapshotting T_wrong's state before and after a rejected cross-tournament
    report and asserting nothing about T_wrong changed.

    We compare 'current_matches' (the projector's main render target) and
    'events' (the ticker). Neither should have any sign of the rejected
    request."""
    tid_a, _ht_a, match_id_a, white_pid_a, _black_pid_a = \
        _make_tournament_with_pending_match(client, "Tournament A")
    tid_b, _ht_b, _match_id_b, _white_pid_b, _black_pid_b = \
        _make_tournament_with_pending_match(client, "Tournament B")

    before = client.get(f"/api/tournaments/{tid_b}/state").json()

    r = client.post(
        f"/api/tournaments/{tid_b}/matches/{match_id_a}/report",
        json={"player_id": white_pid_a, "result": "white"},
    )
    assert r.status_code == 404

    after = client.get(f"/api/tournaments/{tid_b}/state").json()

    # current_matches: same matches, same statuses, same reported_by.
    assert len(after["current_matches"]) == len(before["current_matches"])
    for b_m, a_m in zip(before["current_matches"], after["current_matches"]):
        assert b_m["id"] == a_m["id"]
        assert b_m["status"] == a_m["status"]
        assert b_m["result"] == a_m["result"]
        assert b_m["reported_by"] == a_m["reported_by"]

    # events: no new ticker rows. The _log_event calls in report_result fire
    # AFTER status mutation, but the router-level guard runs first, so no
    # event row should have been written for T_wrong (or T_right, for that
    # matter — but T_right isn't what this test guards).
    assert len(after["events"]) == len(before["events"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))