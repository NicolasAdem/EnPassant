"""
Regression test for Codex audit finding B1: set_match_table() must reject
matches with status='bye'.

Task 7 ("On-site / off-site setting + table numbers") states the invariant
"Byes never get a table_number." The initial seeding paths (start_next_round
and add_manual_match) honor this, but set_match_table() previously only
checked match id / tournament id and would update ANY match — including a
bye row. A host POST to /matches/{bye_mid}/table {"table_number": 7} would
succeed and the projector would then render a phantom "TABLE 7" pill for a
player who has no opponent.

Tests:
    1. POST table on a bye match returns 4xx with a clear error.
    2. The DB row's table_number is still NULL after the rejection.
    3. The non-bye match in the same round still accepts a table number,
       so the fix didn't accidentally close the legitimate path.
    4. Clearing the table number (table_number=null) on a real match still
       works — same code path, also exercises the non-error branch.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def client():
    """Fresh in-process app with a temp SQLite DB. One per test.

    Mirrors the fixture in test_dispute_resolve.py / test_host_token_scrub.py.
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

def _setup_onsite_round_with_bye(client):
    """Create an onsite tournament with 3 players, start round 1.

    3 players guarantees exactly one bye in round 1 regardless of pairing
    algorithm. Returns (tid, host_token, bye_match_id, real_match_id).
    """
    # Onsite tournament (so the table-number path is the real production
    # use case, not a forced-on offsite edge).
    r = client.post(
        "/api/tournaments",
        json={"name": "B1 Regression", "location_mode": "onsite"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    tid, host_token = body["id"], body["host_token"]

    # Three players — odd count forces a bye in round 1.
    for name in ("Alice", "Bob", "Carol"):
        r = client.post(f"/api/tournaments/{tid}/players", json={"name": name})
        assert r.status_code == 200, r.text

    # Start round 1.
    r = client.post(
        f"/api/tournaments/{tid}/rounds",
        params={"host_token": host_token},
        json={},
    )
    assert r.status_code == 200, r.text

    # Pull the matches from state and split into bye / non-bye by status.
    r = client.get(f"/api/tournaments/{tid}/state")
    assert r.status_code == 200, r.text
    matches = r.json()["current_matches"]
    assert len(matches) == 2, f"expected 2 matches (1 real + 1 bye), got {matches}"

    byes = [m for m in matches if m["status"] == "bye"]
    reals = [m for m in matches if m["status"] != "bye"]
    assert len(byes) == 1 and len(reals) == 1, (
        f"expected exactly one bye and one real match, got byes={byes} reals={reals}"
    )
    return tid, host_token, byes[0]["id"], reals[0]["id"]


def _read_match_row(tid, match_id):
    """Read the raw match row from SQLite. Used to confirm a rejected
    request did not mutate state. Same helper as test_dispute_resolve.py."""
    from app.database import db
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM matches WHERE id = ? AND tournament_id = ?",
            (match_id, tid),
        ).fetchone()
        assert row is not None, f"match row {match_id} missing from tournament {tid}"
        return dict(row)


# ---------- Headline regression ----------

def test_set_table_on_bye_is_rejected(client):
    """The bug: a host POST could attach a table_number to a bye match.
    Fix: set_match_table() now rejects status='bye' and the API surfaces
    it as 400."""
    tid, host_token, bye_mid, _real_mid = _setup_onsite_round_with_bye(client)

    # Sanity: the seed path correctly left the bye's table_number NULL.
    pre = _read_match_row(tid, bye_mid)
    assert pre["status"] == "bye"
    assert pre["table_number"] is None, (
        "bye match was seeded with a table_number; seeding bug, not the B1 fix's job"
    )

    # The bug attempt.
    r = client.post(
        f"/api/tournaments/{tid}/matches/{bye_mid}/table",
        params={"host_token": host_token},
        json={"table_number": 7},
    )
    assert r.status_code == 400, (
        f"expected 4xx setting a table on a bye match, got {r.status_code}: {r.text}"
    )
    # Message should make it clear it's the bye that's the problem, not a
    # generic "match not found" misdirection.
    assert "bye" in r.json()["detail"].lower()


def test_set_table_on_bye_does_not_mutate(client):
    """The 400 is half the win — the DB row must also still have NULL."""
    tid, host_token, bye_mid, _real_mid = _setup_onsite_round_with_bye(client)

    r = client.post(
        f"/api/tournaments/{tid}/matches/{bye_mid}/table",
        params={"host_token": host_token},
        json={"table_number": 42},
    )
    assert r.status_code == 400

    after = _read_match_row(tid, bye_mid)
    assert after["table_number"] is None, "table_number mutated despite 4xx"
    assert after["status"] == "bye", "status mutated despite 4xx"


# ---------- Belt-and-braces: real matches still work ----------

def test_set_table_on_real_match_still_works(client):
    """The fix is bye-specific — it must not close the legitimate path."""
    tid, host_token, _bye_mid, real_mid = _setup_onsite_round_with_bye(client)

    r = client.post(
        f"/api/tournaments/{tid}/matches/{real_mid}/table",
        params={"host_token": host_token},
        json={"table_number": 3},
    )
    assert r.status_code == 200, r.text
    assert r.json()["table_number"] == 3

    row = _read_match_row(tid, real_mid)
    assert row["table_number"] == 3


def test_clear_table_on_real_match_still_works(client):
    """Setting table_number=null clears it — same code path, non-error
    branch, makes sure we didn't accidentally collapse the if/else."""
    tid, host_token, _bye_mid, real_mid = _setup_onsite_round_with_bye(client)

    # Set, then clear.
    r = client.post(
        f"/api/tournaments/{tid}/matches/{real_mid}/table",
        params={"host_token": host_token},
        json={"table_number": 5},
    )
    assert r.status_code == 200
    assert _read_match_row(tid, real_mid)["table_number"] == 5

    r = client.post(
        f"/api/tournaments/{tid}/matches/{real_mid}/table",
        params={"host_token": host_token},
        json={"table_number": None},
    )
    assert r.status_code == 200
    assert _read_match_row(tid, real_mid)["table_number"] is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))