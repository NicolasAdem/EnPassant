"""
Regression test for Codex audit finding B3: bye-score paths skip
_recompute_tiebreaks, leaving prior opponents with stale Buchholz/SB
until their own next non-bye match finalizes.

Task 4 invariant: tiebreaks are recomputed on every finalize. _finalize_match
honors this; the two bye-score paths (start_next_round's bye branch and
add_manual_match's black_pid=None branch) did not, so any previous opponent
of the bye'd player saw stale numbers on the projector standings.

Tests:
    1. Swiss path (start_next_round): 3-player tournament, round 1 normal,
       round 2 awards a bye to a player whose round-1 opponent has a
       confirmed game. The opponent's BH/SB must reflect the bye'd player's
       new score immediately after start_next_round returns — not only
       after some later non-bye match of the opponent finalizes.
    2. Manual path (add_manual_match): same shape, but the bye is awarded
       via add_manual_match(white_pid, None) in round 2 of a manual-mode
       tournament. Same invariant — opponent's tiebreaks refresh on the
       spot.

Fixture mirrors test_dispute_resolve.py (temp SQLite per test, monkey-patch
DB_PATH before importing the app). Tests call the service layer directly
rather than going through HTTP because the bug surface is purely service-
level, and add_manual_match has no clean HTTP shape needed for the test.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def fresh_db():
    """Per-test temp SQLite. Patch DB_PATH before init_db() so the schema
    lands in the temp file, not the repo's enpassant.db."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name

    from app import database
    # The dispute-resolve fixture and the test_tiebreaks module both do this
    # same monkey-patch; the path attribute is read every time get_conn()
    # opens a connection, so patching it before init_db() is sufficient.
    database.DB_PATH = db_path
    database.init_db()

    yield db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


# ---------- Helpers ----------

def _read_player(tid, pid):
    """Read a single player's raw row. Used to inspect buchholz / SB after
    the fix-relevant operation, bypassing list_players' ORDER BY (which
    would also be valid, but a direct read makes the failure mode obvious
    in CI output)."""
    from app.database import db
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM players WHERE id = ? AND tournament_id = ?",
            (pid, tid),
        ).fetchone()
        assert row is not None, f"player {pid} missing from tournament {tid}"
        return dict(row)


# ---------- Test 1: Swiss path (start_next_round bye branch) ----------

def test_swiss_round_2_bye_refreshes_round_1_opponent_tiebreaks(fresh_db):
    """3-player Swiss. Round 1: one player byes (Task-6 selector picks them),
    the other two play. Round 2: the round-1 byer has bye_count=1 and is
    excluded, so one of the round-1 real-match players byes — and that
    bye recipient's round-1 opponent is the "stale tiebreak victim" whose
    BH/SB must refresh the instant the bye finalizes.

    Three distinct ELOs (1300/1200/1100) avoid any randomness in tie-
    breaking the bye selector. The test deliberately doesn't pin WHICH
    player byes which round — it reads the round_1 byer and round_2 bye
    recipient from the pairing engine's output, then computes expected
    BH/SB from the actual score landscape. This keeps the test focused on
    the B3 invariant (bye → recompute) rather than on Task-6's specific
    selector ordering, which has its own coverage.

    Headline assertion: after start_next_round returns for round 2, the
    round-2 bye recipient's round-1 opponent has BH/SB consistent with
    the recipient's NEW (post-bye) score. Pre-fix: those numbers lag at
    the old score until a later finalize of the opponent's own match.
    Post-fix: they update immediately, in the same write that bumped the
    bye recipient's score.
    """
    from app.services import tournament as svc

    # Create swiss tournament + three players with distinct ELOs.
    t = svc.create_tournament("B3 Swiss", "swiss")
    tid = t["id"]
    alice = svc.add_player(tid, "Alice", elo=1300)
    bob   = svc.add_player(tid, "Bob",   elo=1200)
    carol = svc.add_player(tid, "Carol", elo=1100)
    # Per-variable narrowing so the type checker sees these as dict, not
    # Optional[dict], on every subsequent subscript. assert all((...))
    # narrows the tuple but not the individual bindings.
    assert alice is not None
    assert bob   is not None
    assert carol is not None

    # Round 1.
    r1 = svc.start_next_round(tid)
    assert r1 is not None, "start_next_round returned None for round 1"
    assert "error" not in r1, r1
    pairings = r1["pairings"]
    # Find the bye and the real match in round 1.
    r1_bye = next((p for p in pairings if p.get("is_bye")), None)
    r1_real = next((p for p in pairings if not p.get("is_bye")), None)
    assert r1_bye is not None, f"expected a bye in round 1 of 3-player swiss; got {pairings}"
    assert r1_real is not None
    # We don't pin WHICH player byes round 1 — the Task-6 selector picks one
    # of the three by (bye_count, score, elo), and any choice satisfies the
    # setup we need for round 2 (the other two played a real match, and the
    # round-1 byer will be excluded from round 2's bye via bye_count). What
    # matters is just that two of the three played each other.
    r1_byer_id = r1_bye["white_player_id"]

    # Confirm the real round-1 match. White wins.
    r1_white = r1_real["white_player_id"]
    r1_black = r1_real["black_player_id"]
    # Find the match id in the current state so we can drive it through
    # the normal report/confirm flow (which is what fires _finalize_match).
    cm = svc.list_current_round_matches(tid)
    real_match = next(m for m in cm if m["status"] == "pending")
    rep = svc.report_result(real_match["id"], r1_white, "white")
    assert rep is not None, "report_result returned None"
    assert rep.get("status") == "reported", rep
    conf = svc.confirm_result(real_match["id"], r1_black, agree=True)
    assert conf is not None, "confirm_result returned None"
    assert conf.get("status") == "confirmed", conf

    # Sanity: whoever byed round 1 got 1 point; the loser of the real
    # match got 0; the winner got 1.
    assert _read_player(tid, r1_byer_id)["score"] == 1.0, "round-1 byer should have 1 pt"
    # One of the two real-match players won (score 1.0), the other lost (0.0).
    winner_id = r1_white  # because we reported "white"
    loser_id  = r1_black
    assert _read_player(tid, winner_id)["score"] == 1.0
    assert _read_player(tid, loser_id)["score"]  == 0.0

    # Now the headline scenario. The round-2 bye recipient will be one of
    # the round-1 real-match players (the round-1 byer is excluded by
    # bye_count). Whichever one, their round-1 opponent's BH/SB depend on
    # the bye recipient's score and must refresh when the bye finalizes.
    loser_bh_before = _read_player(tid, loser_id)["buchholz"]
    winner_bh_before = _read_player(tid, winner_id)["buchholz"]
    winner_sb_before = _read_player(tid, winner_id)["sonneborn_berger"]
    # After round 1, winner's only real opponent is loser (score 0), so
    # winner BH=0 and SB=0. This is the "stale baseline" that pre-fix
    # would persist through round 2's bye if the round-2 bye recipient
    # were the loser.
    assert winner_bh_before == 0.0, f"winner BH before round 2: {winner_bh_before}"
    assert winner_sb_before == 0.0, f"winner SB before round 2: {winner_sb_before}"
    assert loser_bh_before == 1.0   # loser's only opp is winner with score 1

    # Round 2.
    r2 = svc.start_next_round(tid)
    assert r2 is not None, "start_next_round returned None for round 2"
    assert "error" not in r2, r2
    r2_bye = next((p for p in r2["pairings"] if p.get("is_bye")), None)
    assert r2_bye is not None, f"expected a bye in round 2; got {r2['pairings']}"
    r2_bye_recipient = r2_bye["white_player_id"]
    # Whoever byed round 1 has bye_count=1; Task-6 (bye_count ASC) excludes
    # them from round 2's bye. So the round-2 bye goes to one of the two
    # players who played a real match in round 1 — which is exactly what
    # we need to exercise B3 (the bye recipient must have a prior opponent
    # for the "stale tiebreak victim" to exist).
    assert r2_bye_recipient != r1_byer_id, (
        f"player {r1_byer_id} byed round 1; Task-6 bye-fairness should exclude "
        f"them from round 2. If this fires, the bug is in Task 6, not B3 — but "
        "the B3 fix also can't be exercised here without a round-1 participant "
        "getting the round-2 bye."
    )
    assert r2_bye_recipient in (r1_white, r1_black)

    # The other of r1_white/r1_black is the "stale tiebreak victim" — they
    # played the round-2 bye recipient in round 1, and their BH/SB depend
    # on that recipient's score.
    opponent_id = r1_black if r2_bye_recipient == r1_white else r1_white

    # The round-2 bye recipient's score should have jumped by 1.
    recipient_score_now = _read_player(tid, r2_bye_recipient)["score"]
    recipient_score_before_r2 = 1.0 if r2_bye_recipient == winner_id else 0.0
    assert recipient_score_now == recipient_score_before_r2 + 1.0, (
        f"bye recipient score didn't jump as expected: was {recipient_score_before_r2}, "
        f"now {recipient_score_now}"
    )

    # *** THE HEADLINE ASSERTION ***
    # The opponent's BH must equal the bye recipient's NEW score, immediately,
    # without any further finalize being triggered. Pre-fix it'd lag — the
    # bye recipient's score has gone up, but the opponent's Buchholz wasn't
    # recomputed, so it still reads the pre-bye number.
    opponent_after = _read_player(tid, opponent_id)
    assert opponent_after["buchholz"] == recipient_score_now, (
        f"B3 regression: opponent's Buchholz stale after round-2 bye. "
        f"Expected {recipient_score_now} (bye recipient's new score), "
        f"got {opponent_after['buchholz']}. "
        "_recompute_tiebreaks was not called in the start_next_round bye branch."
    )
    # SB = (opponent's game weight vs recipient) * recipient's new score.
    # The opponent beat the recipient iff opponent_id == winner_id, else lost.
    opp_weight = 1.0 if opponent_id == winner_id else 0.0
    expected_sb = opp_weight * recipient_score_now
    assert opponent_after["sonneborn_berger"] == expected_sb, (
        f"B3 regression: opponent's Sonneborn-Berger stale after round-2 bye. "
        f"Expected {expected_sb} (weight {opp_weight} * recipient's new score {recipient_score_now}), "
        f"got {opponent_after['sonneborn_berger']}."
    )


# ---------- Test 2: Manual path (add_manual_match bye branch) ----------

def test_manual_round_2_bye_refreshes_round_1_opponent_tiebreaks(fresh_db):
    """Same invariant as Test 1, but the bye is awarded via add_manual_match
    in a manual-mode tournament. Independent code path; same fix.

    Setup:
      Manual mode, 3 players (Alice, Bob, Carol).
      Round 1 (manual): add Alice-Bob, add Carol-bye. Confirm Alice wins.
        Tiebreaks after r1:
          Alice: opps={Bob}, BH=0, SB=0   (Bob's score is 0)
          Bob:   opps={Alice}, BH=1, SB=0
          Carol: bye doesn't count → BH=0, SB=0
      start_next_round to advance to round 2 (manual mode allows empty
        pairings, so this just opens an empty round).
      Round 2: add_manual_match(Bob, None) — Bob byes. Site 2 fires.
        Bob's score: 0 → 1.
        Pre-fix: Alice's BH stays at 0.
        Post-fix: Alice's BH = 1 (Bob's new score), SB = 1.0 * 1 = 1.
    """
    from app.services import tournament as svc

    t = svc.create_tournament("B3 Manual", "manual")
    tid = t["id"]
    alice = svc.add_player(tid, "Alice")
    bob   = svc.add_player(tid, "Bob")
    carol = svc.add_player(tid, "Carol")
    # Per-variable narrowing (see Test 1 for the rationale).
    assert alice is not None
    assert bob   is not None
    assert carol is not None

    # Round 1: Alice-Bob real, Carol-bye.
    m_ab = svc.add_manual_match(tid, alice["id"], bob["id"])
    assert m_ab is not None, "add_manual_match returned None for Alice vs Bob"
    assert m_ab.get("round_number") == 1, m_ab
    m_cbye = svc.add_manual_match(tid, carol["id"], None)
    assert m_cbye is not None, "add_manual_match returned None for Carol bye"
    assert m_cbye.get("round_number") == 1, m_cbye

    # Confirm Alice-Bob with Alice (white) winning.
    rep = svc.report_result(m_ab["id"], alice["id"], "white")
    assert rep is not None, "report_result returned None"
    assert rep.get("status") == "reported", rep
    conf = svc.confirm_result(m_ab["id"], bob["id"], agree=True)
    assert conf is not None, "confirm_result returned None"
    assert conf.get("status") == "confirmed", conf

    # Baseline tiebreaks.
    p_alice = _read_player(tid, alice["id"])
    p_bob   = _read_player(tid, bob["id"])
    assert p_alice["score"] == 1.0
    assert p_bob["score"]   == 0.0
    # Alice's only opp is Bob (score 0): BH=0, SB=0.
    assert p_alice["buchholz"] == 0.0
    assert p_alice["sonneborn_berger"] == 0.0
    # Bob's only opp is Alice (score 1): BH=1, SB=0 (he lost).
    assert p_bob["buchholz"] == 1.0
    assert p_bob["sonneborn_berger"] == 0.0

    # Advance to round 2. Manual mode + empty pairings is allowed; the
    # round is created and current_round becomes 2.
    r2 = svc.start_next_round(tid)
    assert r2 is not None, "start_next_round returned None for round 2"
    assert "error" not in r2, r2
    assert r2.get("round_number") == 2

    # Round 2: give Bob a bye via add_manual_match. This fires Site 2.
    m_bbye = svc.add_manual_match(tid, bob["id"], None)
    assert m_bbye is not None, "add_manual_match returned None for Bob bye"
    assert m_bbye.get("round_number") == 2, m_bbye

    # Bob's score should now be 1.0 (was 0, +1 for the bye).
    assert _read_player(tid, bob["id"])["score"] == 1.0

    # *** THE HEADLINE ASSERTION ***
    # Alice played Bob in round 1. With Bob's score now 1.0, Alice's BH must
    # be 1.0 and her SB must be 1.0 (she won with weight 1.0). Pre-fix both
    # would still be 0.0.
    alice_after = _read_player(tid, alice["id"])
    assert alice_after["buchholz"] == 1.0, (
        f"B3 regression: Alice's Buchholz stale after Bob's manual bye. "
        f"Expected 1.0 (Bob's new score), got {alice_after['buchholz']}. "
        "_recompute_tiebreaks was not called in the add_manual_match bye branch."
    )
    assert alice_after["sonneborn_berger"] == 1.0, (
        f"B3 regression: Alice's Sonneborn-Berger stale after Bob's manual bye. "
        f"Expected 1.0 (1.0 weight * Bob's new score 1.0), got {alice_after['sonneborn_berger']}."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))