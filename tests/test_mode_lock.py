"""
Session 2: the pairing mode is locked at creation. There is no per-round
override anymore, and a smuggled legacy `mode_override` in the request body is
ignored. Every round records the tournament's chosen mode.

Uses conftest's fresh-user override so each add_player call is a distinct
account (one-player-per-account), and the host acts via host_token.
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


def _round_modes(tid):
    from app.database import db
    with db() as conn:
        rows = conn.execute(
            "SELECT round_number, pairing_mode FROM rounds WHERE tournament_id = ? ORDER BY round_number",
            (tid,),
        ).fetchall()
        return {r["round_number"]: r["pairing_mode"] for r in rows}


def _setup(client, mode, n_players=4):
    body = client.post("/api/tournaments", json={"name": f"{mode} test", "pairing_mode": mode}).json()
    tid, ht = body["id"], body["host_token"]
    assert body["pairing_mode"] == mode
    for i in range(n_players):
        r = client.post(f"/api/tournaments/{tid}/players", json={"name": f"P{i}"})
        assert r.status_code == 200, r.text
    return tid, ht


def test_round_uses_tournament_mode(client):
    tid, ht = _setup(client, "random")
    r = client.post(f"/api/tournaments/{tid}/rounds", params={"host_token": ht}, json={})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "random"
    assert _round_modes(tid)[1] == "random"


def test_legacy_mode_override_in_body_is_ignored(client):
    tid, ht = _setup(client, "random")
    # Old clients used to send {"mode_override": "swiss"}. It must have no effect.
    r = client.post(
        f"/api/tournaments/{tid}/rounds",
        params={"host_token": ht},
        json={"mode_override": "swiss"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "random", "mode_override leaked through the lock"
    assert _round_modes(tid)[1] == "random"


def test_mode_stays_locked_across_multiple_rounds(client):
    tid, ht = _setup(client, "round_robin", n_players=4)
    # Round-robin over 4 players → 3 rounds, all round_robin.
    for expected_round in (1, 2, 3):
        # confirm any prior round's matches first
        state = client.get(f"/api/tournaments/{tid}/state").json()
        for m in state["current_matches"]:
            if m["status"] == "bye":
                continue
            client.post(
                f"/api/tournaments/{tid}/matches/{m['id']}/report",
                json={"player_id": m["white_player_id"], "result": "white"},
            )
            client.post(
                f"/api/tournaments/{tid}/matches/{m['id']}/confirm",
                json={"player_id": m["black_player_id"], "agree": True},
            )
        r = client.post(f"/api/tournaments/{tid}/rounds", params={"host_token": ht}, json={})
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "round_robin"

    modes = _round_modes(tid)
    assert set(modes.values()) == {"round_robin"}, modes


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
