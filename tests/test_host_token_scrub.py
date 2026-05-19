"""
Regression test for Codex audit finding A1: host_token must not leak via
any WebSocket frame or HTTP state response received by non-host subscribers.

Three paths to cover (all of them used to scrub independently; one of them —
broadcast — forgot to):

    1. HTTP GET /api/tournaments/{tid}/state
    2. Initial WS snapshot on /ws/{tid} connect
    3. Broadcast WS frames following a state-mutating action

Fix: get_state_snapshot() now scrubs host_token at the source, so all three
paths are safe by construction.
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def client():
    """Fresh in-process app with a temp SQLite DB. One per test."""
    # Patch DB_PATH BEFORE importing the app so init_db() points at the temp file.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name

    from app import database
    database.DB_PATH = db_path
    # Re-init schema against the new path. init_db is idempotent.
    database.init_db()

    # Import fresh — main.py calls init_db() at import time but we've already
    # repointed DB_PATH, so the schema lands in our temp file.
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _create_tournament(client):
    r = client.post("/api/tournaments", json={"name": "Scrub Test"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Sanity: the creation response IS allowed to return host_token — that's
    # how the host gets it in the first place. The leak is only via state.
    assert "host_token" in body
    return body["id"], body["host_token"]


def _add_player(client, tid, name="Alice"):
    r = client.post(f"/api/tournaments/{tid}/players", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _assert_no_token_anywhere(payload, host_token):
    """Whether the payload is dict, list, or nested, the literal host_token
    string must not appear anywhere in it."""
    serialised = json.dumps(payload)
    assert host_token not in serialised, (
        f"host_token leaked into payload:\n{serialised[:500]}"
    )
    # Also assert the key isn't present at the canonical location.
    if isinstance(payload, dict) and "tournament" in payload:
        assert "host_token" not in payload["tournament"], (
            "host_token key present under .tournament"
        )


# ---------- Path 1: HTTP state endpoint ----------

def test_http_state_endpoint_scrubs_host_token(client):
    tid, host_token = _create_tournament(client)
    r = client.get(f"/api/tournaments/{tid}/state")
    assert r.status_code == 200
    _assert_no_token_anywhere(r.json(), host_token)


# ---------- Path 2: initial WS snapshot ----------

def test_initial_ws_snapshot_scrubs_host_token(client):
    tid, host_token = _create_tournament(client)
    with client.websocket_connect(f"/ws/{tid}") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "state"
        _assert_no_token_anywhere(frame["data"], host_token)


# ---------- Path 3: broadcast frames (the A1 bug) ----------

def test_broadcast_after_join_scrubs_host_token(client):
    """Subscribe a non-host WS, trigger a state-mutating action (player join),
    and verify the resulting broadcast frame carries no host_token."""
    tid, host_token = _create_tournament(client)

    with client.websocket_connect(f"/ws/{tid}") as ws:
        initial = ws.receive_json()  # consume initial snapshot
        _assert_no_token_anywhere(initial["data"], host_token)

        # Mutate state. This triggers _broadcast_state in api.py:add_player.
        _add_player(client, tid, name="Alice")

        # Next frame is the broadcast. Pre-fix, this contained host_token.
        broadcast = ws.receive_json()
        assert broadcast["type"] == "state"
        _assert_no_token_anywhere(broadcast["data"], host_token)


def test_broadcast_after_host_action_scrubs_host_token(client):
    """Same as above but via a host-authenticated mutation (start round).
    Confirms scrub holds even when the action originates from the host."""
    tid, host_token = _create_tournament(client)
    _add_player(client, tid, name="Alice")
    _add_player(client, tid, name="Bob")

    with client.websocket_connect(f"/ws/{tid}") as ws:
        ws.receive_json()  # initial snapshot

        # Drain the two join broadcasts that fired before we connected? No —
        # we connected AFTER the joins, so the initial snapshot already
        # reflects them and no further frames are queued. Now host-start a round.
        r = client.post(
            f"/api/tournaments/{tid}/rounds",
            params={"host_token": host_token},
            json={},
        )
        assert r.status_code == 200, r.text

        broadcast = ws.receive_json()
        assert broadcast["type"] == "state"
        _assert_no_token_anywhere(broadcast["data"], host_token)


# ---------- Belt-and-braces: scrub at the source ----------

def test_get_state_snapshot_directly_scrubs(client):
    """Unit-level: the service function itself must scrub, so any future
    caller (a new endpoint, a script, a debug dump) is safe by default."""
    from app.services import tournament as svc

    tid, host_token = _create_tournament(client)
    snap = svc.get_state_snapshot(tid)
    assert "tournament" in snap
    assert "host_token" not in snap["tournament"]
    assert host_token not in json.dumps(snap)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))