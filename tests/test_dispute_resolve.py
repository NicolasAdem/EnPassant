"""
Regression test for Codex audit finding A3: once a match is disputed, the
only path that can move it is the host's /matches/{mid}/resolve endpoint.

Pre-fix, report_result() rejected only 'confirmed' and 'bye', so a player
could re-report a disputed match. The re-report reset status back to
'reported', and the next confirm_result() finalized it — bypassing host
arbitration entirely.

Tests:
    1. Re-report after dispute is rejected (the headline regression).
    2. The re-report does NOT mutate the match row.
    3. confirm_result() on a disputed match stays closed too (belt-and-
       braces — this was already enforced by confirm_result's "status !=
       reported" guard, but a future refactor of that guard would silently
       reopen the same vulnerability A3 documents).
    4. Host resolve remains the legitimate path out of dispute, and
       transitions disputed -> confirmed cleanly.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def client():
    """Fresh in-process app with a temp SQLite DB. One per test.

    Mirrors the fixture in test_host_token_scrub.py — patch DB_PATH before
    importing the app so init_db() lands the schema in the temp file.
    """
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

def _setup_disputed_match(client):
    """Create a tournament with two players, start round 1, report a result,
    then dispute it. Returns (tid, host_token, match_id, white_pid, black_pid).
    """
    # Tournament
    r = client.post("/api/tournaments", json={"name": "A3 Regression"})
    assert r.status_code == 200, r.text
    body = r.json()
    tid, host_token = body["id"], body["host_token"]

    # Two players
    r = client.post(f"/api/tournaments/{tid}/players", json={"name": "Alice"})
    assert r.status_code == 200, r.text
    alice = r.json()
    r = client.post(f"/api/tournaments/{tid}/players", json={"name": "Bob"})
    assert r.status_code == 200, r.text
    bob = r.json()

    # Start round 1 (host action)
    r = client.post(
        f"/api/tournaments/{tid}/rounds",
        params={"host_token": host_token},
        json={},
    )
    assert r.status_code == 200, r.text
    round_data = r.json()
    pairings = round_data["pairings"]
    assert len(pairings) == 1, "expected exactly one pairing for 2 players"
    pairing = pairings[0]
    white_pid = pairing["white_player_id"]
    black_pid = pairing["black_player_id"]
    assert black_pid is not None, "two-player round should never produce a bye"

    # Find the match id via the current-state endpoint
    r = client.get(f"/api/tournaments/{tid}/state")
    assert r.status_code == 200, r.text
    matches = r.json()["current_matches"]
    assert len(matches) == 1
    match_id = matches[0]["id"]

    # White reports a white win
    r = client.post(
        f"/api/tournaments/{tid}/matches/{match_id}/report",
        json={"player_id": white_pid, "result": "white"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reported"

    # Black disputes
    r = client.post(
        f"/api/tournaments/{tid}/matches/{match_id}/confirm",
        json={"player_id": black_pid, "agree": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "disputed"

    return tid, host_token, match_id, white_pid, black_pid


def _read_match_row(tid, match_id):
    """Read the raw match row from SQLite. Used to confirm a rejected
    request did not mutate state.

    Asserts the row exists — every caller in this file needs the row to
    be present (a destructive bug that deleted it would itself be a
    regression worth failing on), and the assertion narrows the return
    type from Optional[dict] to dict so callers can subscript directly.
    """
    from app.database import db
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM matches WHERE id = ? AND tournament_id = ?",
            (match_id, tid),
        ).fetchone()
        assert row is not None, f"match row {match_id} missing from tournament {tid}"
        return dict(row)


# ---------- Headline regression ----------

def test_re_report_after_dispute_is_rejected(client):
    """The bug: a disputed match could be re-reported, silently resetting
    status to 'reported' and reopening the confirm path. Fix: report_result
    now rejects status='disputed'."""
    tid, _host_token, match_id, white_pid, _black_pid = _setup_disputed_match(client)

    # White (the original reporter) tries to re-report. Pre-fix: 200 + status
    # flips back to 'reported'. Post-fix: 400 with an error explaining that
    # only the host can resolve.
    r = client.post(
        f"/api/tournaments/{tid}/matches/{match_id}/report",
        json={"player_id": white_pid, "result": "white"},
    )
    assert r.status_code == 400, (
        f"expected 4xx on re-report of disputed match, got {r.status_code}: {r.text}"
    )
    # Message should make it clear that the host is the path forward, not
    # just "already finalized" (which would be misleading — a disputed match
    # is the opposite of finalized).
    assert "host" in r.json()["detail"].lower()


def test_re_report_after_dispute_does_not_mutate(client):
    """The 400 is only half the win — we also need to confirm the DB row
    actually didn't change. Pre-fix, the UPDATE ran before any guard could
    catch it; post-fix the guard runs first and returns early."""
    tid, _host_token, match_id, white_pid, _black_pid = _setup_disputed_match(client)

    before = _read_match_row(tid, match_id)
    assert before["status"] == "disputed"

    # Attempt the re-report with a DIFFERENT result, so any silent mutation
    # would visibly flip the 'result' column.
    r = client.post(
        f"/api/tournaments/{tid}/matches/{match_id}/report",
        json={"player_id": white_pid, "result": "draw"},
    )
    assert r.status_code == 400

    after = _read_match_row(tid, match_id)
    assert after["status"] == "disputed", "status changed despite 4xx"
    assert after["result"] == before["result"], "result mutated despite 4xx"
    assert after["reported_by"] == before["reported_by"], "reported_by mutated despite 4xx"


def test_disputing_opponent_also_cannot_re_report(client):
    """Symmetry check: the player who *disputed* shouldn't be able to
    backdoor a result in either. Same guard catches both."""
    tid, _host_token, match_id, _white_pid, black_pid = _setup_disputed_match(client)

    r = client.post(
        f"/api/tournaments/{tid}/matches/{match_id}/report",
        json={"player_id": black_pid, "result": "black"},
    )
    assert r.status_code == 400
    assert _read_match_row(tid, match_id)["status"] == "disputed"


# ---------- Belt-and-braces: confirm is also closed ----------

def test_confirm_on_disputed_match_is_rejected(client):
    """confirm_result already rejects anything that isn't 'reported', so a
    disputed match can't be confirmed via that route either. This was true
    pre-fix as well; the A3 bug only existed because report_result reopened
    the path. We assert this here so a future refactor of confirm_result's
    status guard doesn't silently reintroduce the same vulnerability."""
    tid, _host_token, match_id, _white_pid, black_pid = _setup_disputed_match(client)

    r = client.post(
        f"/api/tournaments/{tid}/matches/{match_id}/confirm",
        json={"player_id": black_pid, "agree": True},
    )
    assert r.status_code == 400
    assert _read_match_row(tid, match_id)["status"] == "disputed"


# ---------- The legitimate exit still works ----------

def test_host_resolve_finalizes_disputed_match(client):
    """Host arbitration is the ONLY way out of a dispute, and it works."""
    tid, host_token, match_id, _white_pid, _black_pid = _setup_disputed_match(client)

    r = client.post(
        f"/api/tournaments/{tid}/matches/{match_id}/resolve",
        params={"host_token": host_token},
        json={"result": "draw"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirmed"

    row = _read_match_row(tid, match_id)
    assert row["status"] == "confirmed"
    assert row["result"] == "draw"
    assert row["confirmed_by"] == "host"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))