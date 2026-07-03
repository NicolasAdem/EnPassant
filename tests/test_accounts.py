"""
End-to-end coverage for session-1 accounts: signup, login, sessions, the
login gate on create/join, the one-player-per-account rule, and the lobby
summary the home page renders.

These drive the real /api/auth endpoints with real session cookies, so they
opt out of conftest's fresh-user dependency override via @pytest.mark.real_auth.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.real_auth


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

    # TestClient keeps a cookie jar, so a signup/login persists across requests
    # on the same client — exactly like a browser session.
    with TestClient(app) as c:
        yield c

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _signup(client, email="alice@example.com", pw="password123", name="Alice"):
    return client.post("/api/auth/signup", json={"email": email, "password": pw, "display_name": name})


# ---------- signup / login / session ----------

def test_signup_sets_session_and_me_works(client):
    r = _signup(client)
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Alice"
    assert "ep_session" in r.cookies

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


def test_duplicate_email_rejected(client):
    assert _signup(client).status_code == 200
    r = _signup(client, name="Alice2")  # same email
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"].lower()


def test_short_password_rejected(client):
    r = _signup(client, pw="short")
    assert r.status_code == 400


def test_login_wrong_password_rejected(client):
    _signup(client)
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": "alice@example.com", "password": "nope"})
    assert r.status_code == 400


def test_login_roundtrip(client):
    _signup(client)
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401
    r = client.post("/api/auth/login", json={"email": "alice@example.com", "password": "password123"})
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_logout_clears_session(client):
    _signup(client)
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


# ---------- login gate ----------

def test_create_tournament_requires_login(client):
    r = client.post("/api/tournaments", json={"name": "No Auth"})
    assert r.status_code == 401


def test_join_requires_login(client):
    # Host creates a tournament...
    _signup(client, email="host@example.com", name="Host")
    tid = client.post("/api/tournaments", json={"name": "Gated"}).json()["id"]
    client.post("/api/auth/logout")
    # ...an anonymous client cannot add a player.
    r = client.post(f"/api/tournaments/{tid}/players", json={"name": "Ghost"})
    assert r.status_code == 401


# ---------- one player per account ----------

def test_one_player_per_account_is_idempotent(client):
    _signup(client)
    tid = client.post("/api/tournaments", json={"name": "Solo"}).json()["id"]
    p1 = client.post(f"/api/tournaments/{tid}/players", json={"name": "Alice"})
    p2 = client.post(f"/api/tournaments/{tid}/players", json={"name": "Alice Again"})
    assert p1.status_code == 200 and p2.status_code == 200
    # Same account → same player row, not a duplicate.
    assert p1.json()["id"] == p2.json()["id"]

    state = client.get(f"/api/tournaments/{tid}/state").json()
    assert len(state["players"]) == 1


# ---------- lobbies ----------

def test_lobbies_list_hosting_and_playing(client):
    from app.services import tournament as svc

    # Host account creates a tournament and also joins it as a player.
    host = _signup(client, email="host@example.com", name="Host").json()
    tid = client.post("/api/tournaments", json={"name": "Friday Blitz"}).json()["id"]
    client.post(f"/api/tournaments/{tid}/players", json={"name": "Host"})

    lob = svc.lobbies_for_user(host["id"])
    assert len(lob["hosting"]) == 1
    assert lob["hosting"][0]["name"] == "Friday Blitz"
    assert lob["hosting"][0]["player_count"] == 1
    assert len(lob["playing"]) == 1
    assert lob["playing"][0]["state"] == "waiting_start"


def test_lobby_status_shows_ready_match(client):
    from app.services import tournament as svc

    _signup(client, email="h@example.com", name="H")
    tid = client.post("/api/tournaments", json={"name": "Match Ready"}).json()["id"]
    # Two distinct accounts join so a real pairing can be made.
    client.post(f"/api/tournaments/{tid}/players", json={"name": "H"})
    host_token = svc.get_tournament(tid)["host_token"]

    client.post("/api/auth/logout")
    _signup(client, email="rival@example.com", name="Rival")
    rival_player = client.post(f"/api/tournaments/{tid}/players", json={"name": "Rival"}).json()

    # Host starts round 1.
    r = client.post(f"/api/tournaments/{tid}/rounds", params={"host_token": host_token}, json={})
    assert r.status_code == 200, r.text

    # Rival's lobby should now show a ready match.
    rival_user = client.get("/api/auth/me").json()
    lob = svc.lobbies_for_user(rival_user["id"])
    playing = lob["playing"][0]
    assert playing["state"] in ("ready", "bye")
    if playing["state"] == "ready":
        assert "vs" in playing["summary"]


# ---------- rename ----------

def test_rename_tournament_requires_host_and_updates_state(client):
    _signup(client)
    body = client.post("/api/tournaments", json={"name": "Old Name"}).json()
    tid, ht = body["id"], body["host_token"]

    # Without the host token → 403.
    assert client.post(f"/api/tournaments/{tid}/name", json={"name": "New Name"}).status_code == 403

    # With it → renamed, and the change shows up in state (drives the broadcast).
    r = client.post(f"/api/tournaments/{tid}/name", params={"host_token": ht}, json={"name": "New Name"})
    assert r.status_code == 200
    assert r.json()["name"] == "New Name"
    assert client.get(f"/api/tournaments/{tid}/state").json()["tournament"]["name"] == "New Name"


def test_rename_empty_rejected(client):
    _signup(client)
    body = client.post("/api/tournaments", json={"name": "Keep"}).json()
    tid, ht = body["id"], body["host_token"]
    r = client.post(f"/api/tournaments/{tid}/name", params={"host_token": ht}, json={"name": "   "})
    assert r.status_code == 400
    assert client.get(f"/api/tournaments/{tid}/state").json()["tournament"]["name"] == "Keep"


# ---------- background image ----------

def test_set_and_clear_background(client):
    _signup(client)
    body = client.post("/api/tournaments", json={"name": "Bg"}).json()
    tid, ht = body["id"], body["host_token"]

    r = client.post(f"/api/tournaments/{tid}/background", params={"host_token": ht},
                    json={"url": "https://example.com/pic.jpg"})
    assert r.status_code == 200
    assert r.json()["background_url"] == "https://example.com/pic.jpg"
    assert client.get(f"/api/tournaments/{tid}/state").json()["tournament"]["background_url"] == "https://example.com/pic.jpg"

    # Clearing with an empty string sets it back to null.
    r = client.post(f"/api/tournaments/{tid}/background", params={"host_token": ht}, json={"url": ""})
    assert r.status_code == 200
    assert r.json()["background_url"] is None


def test_background_rejects_non_http_and_injection(client):
    _signup(client)
    body = client.post("/api/tournaments", json={"name": "Bg"}).json()
    tid, ht = body["id"], body["host_token"]
    for bad in ["javascript:alert(1)", 'https://x/a")fake', "ftp://x/y.png", "https://x/a b.png"]:
        r = client.post(f"/api/tournaments/{tid}/background", params={"host_token": ht}, json={"url": bad})
        assert r.status_code == 400, bad


def test_background_requires_host(client):
    _signup(client)
    tid = client.post("/api/tournaments", json={"name": "Bg"}).json()["id"]
    r = client.post(f"/api/tournaments/{tid}/background", json={"url": "https://example.com/p.png"})
    assert r.status_code == 403


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
