"""
Session 6: automatic round advancement. When auto_rounds is enabled, the server
starts the next round on its own the moment the last match of the current round
is confirmed. When it's off, confirming the last match does nothing extra.

Uses conftest's fresh-user override so each add_player is a distinct account.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def client():
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


def _setup(client, n=4):
    body = client.post("/api/tournaments", json={"name": "Auto", "pairing_mode": "swiss"}).json()
    tid, ht = body["id"], body["host_token"]
    for i in range(n):
        client.post(f"/api/tournaments/{tid}/players", json={"name": f"P{i}"})
    return tid, ht


def _confirm_all_current(client, tid):
    """Report + confirm every non-bye match in the current round."""
    state = client.get(f"/api/tournaments/{tid}/state").json()
    for m in state["current_matches"]:
        if m["status"] == "bye":
            continue
        client.post(f"/api/tournaments/{tid}/matches/{m['id']}/report",
                    json={"player_id": m["white_player_id"], "result": "white"})
        client.post(f"/api/tournaments/{tid}/matches/{m['id']}/confirm",
                    json={"player_id": m["black_player_id"], "agree": True})


def _current_round(client, tid):
    return client.get(f"/api/tournaments/{tid}/state").json()["tournament"]["current_round"]


def test_auto_rounds_advances_when_round_completes(client):
    tid, ht = _setup(client, 4)
    assert client.post(f"/api/tournaments/{tid}/auto_rounds", params={"host_token": ht},
                       json={"enabled": True}).status_code == 200
    client.post(f"/api/tournaments/{tid}/rounds", params={"host_token": ht}, json={})
    assert _current_round(client, tid) == 1

    _confirm_all_current(client, tid)
    # All round-1 matches confirmed → the server should have auto-started round 2.
    assert _current_round(client, tid) == 2


def test_auto_rounds_does_not_advance_midround(client):
    tid, ht = _setup(client, 4)
    client.post(f"/api/tournaments/{tid}/auto_rounds", params={"host_token": ht}, json={"enabled": True})
    client.post(f"/api/tournaments/{tid}/rounds", params={"host_token": ht}, json={})

    # Confirm only ONE of the two boards → round is not complete → no advance.
    state = client.get(f"/api/tournaments/{tid}/state").json()
    m = next(x for x in state["current_matches"] if x["status"] != "bye")
    client.post(f"/api/tournaments/{tid}/matches/{m['id']}/report",
                json={"player_id": m["white_player_id"], "result": "white"})
    client.post(f"/api/tournaments/{tid}/matches/{m['id']}/confirm",
                json={"player_id": m["black_player_id"], "agree": True})
    assert _current_round(client, tid) == 1


def test_no_auto_advance_when_disabled(client):
    tid, ht = _setup(client, 4)
    client.post(f"/api/tournaments/{tid}/rounds", params={"host_token": ht}, json={})
    _confirm_all_current(client, tid)
    # Auto-rounds off (default) → still round 1, host must advance manually.
    assert _current_round(client, tid) == 1


def test_auto_rounds_toggle_reflected_in_state(client):
    tid, ht = _setup(client, 2)
    assert client.get(f"/api/tournaments/{tid}/state").json()["tournament"]["auto_rounds"] == 0
    client.post(f"/api/tournaments/{tid}/auto_rounds", params={"host_token": ht}, json={"enabled": True})
    assert client.get(f"/api/tournaments/{tid}/state").json()["tournament"]["auto_rounds"] == 1


def test_auto_rounds_requires_host(client):
    tid, _ht = _setup(client, 2)
    r = client.post(f"/api/tournaments/{tid}/auto_rounds", json={"enabled": True})
    assert r.status_code == 403


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
