"""
Teams mode (session 4): team creation + validation, team-required joins,
cross-team pairing, and host reassignment.

Uses conftest's fresh-user-per-request override, so each /players call is a
distinct account — which is exactly what we need to populate two teams.
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


def _make_team_tournament(client, names=("Reds", "Blues")):
    r = client.post("/api/tournaments", json={"name": "Club Match", "teams": list(names)})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    ht = r.json()["host_token"]
    teams = client.get(f"/api/tournaments/{tid}/state").json()["teams"]
    return tid, ht, teams


# ---------- creation / validation ----------

def test_create_with_teams(client):
    tid, _ht, teams = _make_team_tournament(client)
    assert [t["name"] for t in teams] == ["Reds", "Blues"]
    # Distinct auto-assigned colors.
    assert teams[0]["color"] != teams[1]["color"]


def test_team_count_validation(client):
    assert client.post("/api/tournaments", json={"name": "x", "teams": ["Solo"]}).status_code == 400
    assert client.post("/api/tournaments", json={"name": "x", "teams": ["a", "b", "c", "d", "e"]}).status_code == 400


# ---------- joining ----------

def test_join_requires_team(client):
    tid, _ht, _teams = _make_team_tournament(client)
    # No team → rejected.
    r = client.post(f"/api/tournaments/{tid}/players", json={"name": "Ann"})
    assert r.status_code == 400
    assert "team" in r.json()["detail"].lower()


def test_join_with_valid_team(client):
    tid, _ht, teams = _make_team_tournament(client)
    r = client.post(f"/api/tournaments/{tid}/players", json={"name": "Ann", "team_id": teams[0]["id"]})
    assert r.status_code == 200
    assert r.json()["team_id"] == teams[0]["id"]


def test_join_invalid_team_rejected(client):
    tid, _ht, _teams = _make_team_tournament(client)
    r = client.post(f"/api/tournaments/{tid}/players", json={"name": "Ann", "team_id": "not-a-team"})
    assert r.status_code == 400


def test_non_team_tournament_ignores_team_id(client):
    tid = client.post("/api/tournaments", json={"name": "Plain"}).json()["id"]
    r = client.post(f"/api/tournaments/{tid}/players", json={"name": "Ann", "team_id": "whatever"})
    assert r.status_code == 200
    assert r.json()["team_id"] is None


# ---------- cross-team pairing ----------

def test_swiss_round_is_cross_team(client):
    tid, ht, teams = _make_team_tournament(client)
    red, blue = teams[0]["id"], teams[1]["id"]
    for name, team in [("R1", red), ("R2", red), ("B1", blue), ("B2", blue)]:
        assert client.post(f"/api/tournaments/{tid}/players", json={"name": name, "team_id": team}).status_code == 200

    r = client.post(f"/api/tournaments/{tid}/rounds", params={"host_token": ht}, json={})
    assert r.status_code == 200, r.text

    state = client.get(f"/api/tournaments/{tid}/state").json()
    team_of = {p["id"]: p["team_id"] for p in state["players"]}
    boards = [m for m in state["current_matches"] if m["status"] != "bye"]
    assert boards, "expected at least one real board"
    for m in boards:
        assert team_of[m["white_player_id"]] != team_of[m["black_player_id"]], \
            "teammates were paired against each other"


# ---------- reassignment ----------

def test_host_reassign_team_in_lobby(client):
    tid, ht, teams = _make_team_tournament(client)
    red, blue = teams[0]["id"], teams[1]["id"]
    pid = client.post(f"/api/tournaments/{tid}/players", json={"name": "Ann", "team_id": red}).json()["id"]

    r = client.post(f"/api/tournaments/{tid}/players/{pid}/team", params={"host_token": ht}, json={"team_id": blue})
    assert r.status_code == 200

    state = client.get(f"/api/tournaments/{tid}/state").json()
    moved = next(p for p in state["players"] if p["id"] == pid)
    assert moved["team_id"] == blue


def test_reassign_requires_host(client):
    tid, _ht, teams = _make_team_tournament(client)
    pid = client.post(f"/api/tournaments/{tid}/players", json={"name": "Ann", "team_id": teams[0]["id"]}).json()["id"]
    r = client.post(f"/api/tournaments/{tid}/players/{pid}/team", json={"team_id": teams[1]["id"]})
    assert r.status_code == 403


def test_reassign_blocked_after_start(client):
    tid, ht, teams = _make_team_tournament(client)
    red, blue = teams[0]["id"], teams[1]["id"]
    ids = []
    for name, team in [("R1", red), ("B1", blue)]:
        ids.append(client.post(f"/api/tournaments/{tid}/players", json={"name": name, "team_id": team}).json()["id"])
    assert client.post(f"/api/tournaments/{tid}/rounds", params={"host_token": ht}, json={}).status_code == 200
    # Tournament is active now → reassignment refused.
    r = client.post(f"/api/tournaments/{tid}/players/{ids[0]}/team", params={"host_token": ht}, json={"team_id": blue})
    assert r.status_code == 400


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
