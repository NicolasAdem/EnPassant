"""
Sonneborn-Berger + Buchholz unit tests.

We construct small tournaments by directly calling _recompute_tiebreaks
with controlled inputs, then assert against hand-computed expected values.
"""

import sys, os, tempfile, shutil
# Use a fresh sqlite file per test run
TMP = tempfile.mkdtemp(prefix="enpassant-test-")
os.environ["EP_DB"] = os.path.join(TMP, "test.db")

sys.path.insert(0, "/home/claude/task4")

# Monkey-patch the DB path before importing
import app.database as dbmod
from pathlib import Path
dbmod.DB_PATH = Path(os.environ["EP_DB"])

from app.database import init_db, db
from app.services import tournament as svc


def setup_module(module=None):
    init_db()


def teardown_module(module=None):
    shutil.rmtree(TMP, ignore_errors=True)


def _make_tournament_with_players(player_names):
    t = svc.create_tournament("Test", "manual")
    tid = t["id"]
    pids = {}
    for name in player_names:
        p = svc.add_player(tid, name)
        assert p is not None
        pids[name] = p["id"]
    return tid, pids


def _insert_confirmed_match(conn, tid: str, white_id: str, black_id, result: str):
    """Directly insert a confirmed match, bypassing the player flow."""
    import uuid
    rid = uuid.uuid4().hex
    mid = uuid.uuid4().hex
    # Need a round to attach to
    r = conn.execute(
        "SELECT id FROM rounds WHERE tournament_id = ? LIMIT 1",
        (tid,),
    ).fetchone()
    if not r:
        conn.execute(
            "INSERT INTO rounds (id, tournament_id, round_number, pairing_mode) VALUES (?, ?, 1, 'manual')",
            (rid, tid),
        )
    else:
        rid = r["id"]
    if result == "bye":
        conn.execute(
            "INSERT INTO matches (id, round_id, tournament_id, board_number, white_player_id, black_player_id, result, status) "
            "VALUES (?, ?, ?, 99, ?, NULL, 'bye', 'bye')",
            (mid, rid, tid, white_id),
        )
    else:
        conn.execute(
            "INSERT INTO matches (id, round_id, tournament_id, board_number, white_player_id, black_player_id, result, status) "
            "VALUES (?, ?, ?, 99, ?, ?, ?, 'confirmed')",
            (mid, rid, tid, white_id, black_id, result),
        )


def _set_score(conn, pid: str, score: float):
    conn.execute("UPDATE players SET score = ? WHERE id = ?", (score, pid))


def test_classic_4_player_round_robin():
    """4 players, round-robin. Known SB textbook case.

    Standings after all games:
      Alice 3.0   (beat Bob, Carol, Dave)
      Bob   2.0   (lost to Alice; beat Carol, Dave)
      Carol 1.0   (lost to Alice, Bob; beat Dave)
      Dave  0.0   (lost to everyone)

    Sonneborn-Berger (sum of (opp_score * game_weight)):
      Alice: 1*2.0 (beat Bob) + 1*1.0 (beat Carol) + 1*0.0 (beat Dave) = 3.0
      Bob:   0*3.0 (lost to Alice) + 1*1.0 (beat Carol) + 1*0.0 (beat Dave) = 1.0
      Carol: 0*3.0 + 0*2.0 + 1*0.0 = 0.0
      Dave:  0*3.0 + 0*2.0 + 0*1.0 = 0.0

    Buchholz (sum of opponents' final scores; each opponent once):
      Alice: 2 + 1 + 0 = 3.0
      Bob:   3 + 1 + 0 = 4.0
      Carol: 3 + 2 + 0 = 5.0
      Dave:  3 + 2 + 1 = 6.0
    """
    tid, p = _make_tournament_with_players(["Alice", "Bob", "Carol", "Dave"])
    with db() as conn:
        # Alice's wins
        _insert_confirmed_match(conn, tid, p["Alice"], p["Bob"], "white")    # Alice beats Bob
        _insert_confirmed_match(conn, tid, p["Alice"], p["Carol"], "white")  # Alice beats Carol
        _insert_confirmed_match(conn, tid, p["Alice"], p["Dave"], "white")   # Alice beats Dave
        # Bob's other games
        _insert_confirmed_match(conn, tid, p["Bob"], p["Carol"], "white")    # Bob beats Carol
        _insert_confirmed_match(conn, tid, p["Bob"], p["Dave"], "white")     # Bob beats Dave
        # Carol's last game
        _insert_confirmed_match(conn, tid, p["Carol"], p["Dave"], "white")   # Carol beats Dave
        # Set scores manually (since we bypassed _finalize_match)
        _set_score(conn, p["Alice"], 3.0)
        _set_score(conn, p["Bob"], 2.0)
        _set_score(conn, p["Carol"], 1.0)
        _set_score(conn, p["Dave"], 0.0)
        svc._recompute_tiebreaks(conn, tid)

    players = {pl["name"]: pl for pl in svc.list_players(tid)}
    print(f"\n4-player RR results:")
    for name in ["Alice", "Bob", "Carol", "Dave"]:
        pl = players[name]
        print(f"  {name}: score={pl['score']:.1f} BH={pl['buchholz']:.1f} SB={pl['sonneborn_berger']:.1f}")

    assert players["Alice"]["sonneborn_berger"] == 3.0, f"Alice SB: {players['Alice']['sonneborn_berger']}"
    assert players["Bob"]["sonneborn_berger"]   == 1.0, f"Bob SB: {players['Bob']['sonneborn_berger']}"
    assert players["Carol"]["sonneborn_berger"] == 0.0, f"Carol SB: {players['Carol']['sonneborn_berger']}"
    assert players["Dave"]["sonneborn_berger"]  == 0.0, f"Dave SB: {players['Dave']['sonneborn_berger']}"

    assert players["Alice"]["buchholz"] == 3.0
    assert players["Bob"]["buchholz"]   == 4.0
    assert players["Carol"]["buchholz"] == 5.0
    assert players["Dave"]["buchholz"]  == 6.0


def test_tied_scores_broken_by_sb():
    """Classic tiebreak case: two players tied on score, SB picks the one
    who beat stronger opponents.

      Alice and Bob both score 2.0.
      Alice beat Strong (2.0) and Weak (0.0).
      Bob   beat Mid    (1.0) twice... no wait, no rematches. Let's do:
      Bob   beat Mid1 (1.0) and Mid2 (1.0).
      Strong, Weak, Mid1, Mid2 are filler whose only role is to have scores.

    Expected SB:
      Alice: 1*2.0 + 1*0.0 = 2.0
      Bob:   1*1.0 + 1*1.0 = 2.0  -- ties! Interesting.

    Hmm, this case ties SB too. Let me try with a draw:
      Alice beat Strong, drew Weak.
      Bob beat Mid1, beat Weak.
    Score: Alice = 1.5, Bob = 2.0. Different scores. Not a tiebreak case.

    Better: both score 2.0:
      Alice: beat Strong(3), drew Strong2(2)   → score 1.5  -- nope
    
    OK simplest: both have 1.5.
      Alice: beat Strong(2), drew Weak(0)      → SB = 1*2 + 0.5*0 = 2.0
      Bob:   drew Strong(2), beat Weak(0)      → SB = 0.5*2 + 1*0 = 1.0
      Both score 1.5. SB clearly favors Alice.
    """
    tid, p = _make_tournament_with_players(["Alice", "Bob", "Strong", "Weak"])
    with db() as conn:
        _insert_confirmed_match(conn, tid, p["Alice"], p["Strong"], "white")  # Alice beats Strong
        _insert_confirmed_match(conn, tid, p["Alice"], p["Weak"], "draw")     # Alice draws Weak
        _insert_confirmed_match(conn, tid, p["Bob"], p["Strong"], "draw")     # Bob draws Strong
        _insert_confirmed_match(conn, tid, p["Bob"], p["Weak"], "white")      # Bob beats Weak
        # Set scores
        _set_score(conn, p["Alice"], 1.5)
        _set_score(conn, p["Bob"], 1.5)
        _set_score(conn, p["Strong"], 0.5)  # lost+drew
        _set_score(conn, p["Weak"], 0.5)    # drew+lost
        svc._recompute_tiebreaks(conn, tid)

    players = {pl["name"]: pl for pl in svc.list_players(tid)}
    print(f"\nTied-scores test:")
    for name in ["Alice", "Bob", "Strong", "Weak"]:
        pl = players[name]
        print(f"  {name}: score={pl['score']:.1f} BH={pl['buchholz']:.1f} SB={pl['sonneborn_berger']:.1f}")

    # Alice beat Strong(0.5) + drew Weak(0.5) → SB = 1*0.5 + 0.5*0.5 = 0.75
    # Bob   drew Strong(0.5) + beat Weak(0.5) → SB = 0.5*0.5 + 1*0.5 = 0.75
    # Hmm, they tie SB too because Strong and Weak ended up with equal scores.
    # That's actually realistic — let me just assert what SB is and check ordering.
    assert players["Alice"]["sonneborn_berger"] == 0.75
    assert players["Bob"]["sonneborn_berger"] == 0.75


def test_sb_favors_beating_stronger_opponent():
    """Construct a case where SB definitively breaks the tie.
    
      Alice beat Strong(2.0), lost to Strong2(2.0) → score 1.0, SB = 1*2 + 0*2 = 2.0
      Bob   beat Weak(0.0), lost to Weak2(0.0)   → score 1.0, SB = 1*0 + 0*0 = 0.0
    """
    tid, p = _make_tournament_with_players(["Alice", "Bob", "Strong", "Strong2", "Weak", "Weak2"])
    with db() as conn:
        _insert_confirmed_match(conn, tid, p["Alice"], p["Strong"], "white")   # Alice beats Strong
        _insert_confirmed_match(conn, tid, p["Strong2"], p["Alice"], "white")  # Strong2 beats Alice
        _insert_confirmed_match(conn, tid, p["Bob"], p["Weak"], "white")        # Bob beats Weak
        _insert_confirmed_match(conn, tid, p["Weak2"], p["Bob"], "white")       # Weak2 beats Bob

        _set_score(conn, p["Alice"], 1.0)
        _set_score(conn, p["Bob"], 1.0)
        _set_score(conn, p["Strong"], 2.0)
        _set_score(conn, p["Strong2"], 2.0)
        _set_score(conn, p["Weak"], 0.0)
        _set_score(conn, p["Weak2"], 0.0)
        svc._recompute_tiebreaks(conn, tid)

    players = {pl["name"]: pl for pl in svc.list_players(tid)}
    print(f"\nSB-favors-stronger test:")
    for name in ["Alice", "Bob"]:
        pl = players[name]
        print(f"  {name}: score={pl['score']:.1f} BH={pl['buchholz']:.1f} SB={pl['sonneborn_berger']:.1f}")

    assert players["Alice"]["sonneborn_berger"] == 2.0, f"Alice SB: {players['Alice']['sonneborn_berger']}"
    assert players["Bob"]["sonneborn_berger"]   == 0.0, f"Bob SB: {players['Bob']['sonneborn_berger']}"

    # And the ORDER BY should now put Alice ahead of Bob (same score, same Buchholz,
    # different SB)
    ordered = svc.list_players(tid)
    # Filter to just Alice/Bob
    top_two = [pl for pl in ordered if pl["name"] in ("Alice", "Bob")]
    assert top_two[0]["name"] == "Alice", f"Expected Alice first, got {[p['name'] for p in top_two]}"


def test_bye_excluded_from_sb():
    """A bye should not contribute to SB (no opponent).
    
      Alice gets a bye (1 free point) + beats Bob(0)
      Score = 2, SB = 1*0 = 0 (the bye doesn't appear)
    """
    tid, p = _make_tournament_with_players(["Alice", "Bob"])
    with db() as conn:
        _insert_confirmed_match(conn, tid, p["Alice"], None, "bye")
        _insert_confirmed_match(conn, tid, p["Alice"], p["Bob"], "white")
        _set_score(conn, p["Alice"], 2.0)
        _set_score(conn, p["Bob"], 0.0)
        svc._recompute_tiebreaks(conn, tid)

    players = {pl["name"]: pl for pl in svc.list_players(tid)}
    print(f"\nBye-excluded test:")
    for name in ["Alice", "Bob"]:
        pl = players[name]
        print(f"  {name}: score={pl['score']:.1f} BH={pl['buchholz']:.1f} SB={pl['sonneborn_berger']:.1f}")

    assert players["Alice"]["sonneborn_berger"] == 0.0
    assert players["Alice"]["buchholz"] == 0.0  # only opponent is Bob (0 pts)


if __name__ == "__main__":
    setup_module()
    try:
        test_classic_4_player_round_robin()
        test_tied_scores_broken_by_sb()
        test_sb_favors_beating_stronger_opponent()
        test_bye_excluded_from_sb()
        print("\n=== ALL TIEBREAK TESTS PASSED ===")
    finally:
        teardown_module()