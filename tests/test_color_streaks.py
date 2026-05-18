"""
Task #11: color balance refinement — avoid three-in-a-row of one color.

Tests:
  a. Unit: _color_streak counts trailing same-color runs correctly, with
     bye-neutrality.
  b. Unit: _assign_colors gives the opposite color to a player on a 2-streak
     when the opponent isn't on a competing streak.
  c. Unit: when both players are on OPPOSITE-color 2-streaks, each gets
     their preferred opposite (assignment is forced and compatible).
  d. Unit: when both players are on the SAME-color 2-streak, the function
     falls through to cumulative balance — one of them must extend to 3,
     and the choice is deterministic (lower-id breaks tie when whites are
     equal).
  e. Unit: a bye in the middle of a streak doesn't reset it (FIDE
     convention for absolute color preference).
  f. Regression: with no streaks (no prior games), behavior is identical
     to the pre-task-11 version — fewer-whites wins, ties go to
     more-blacks, final fallback is lex id.
  g. End-to-end: in a 16-player 12-round white-wins simulation, every 3+
     streak that DOES happen is "forced" — both players in that pairing
     were on a same-color 2-streak before the round. There are no
     avoidable 3-streaks.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from app.services.pairing import _color_streak, _assign_colors
from test_swiss import simulate


def _player(pid, score=0.0, elo=1500):
    return {"id": pid, "name": pid, "score": score, "elo": elo}


def _white(p, opp):
    """Helper to build a 'p played white vs opp' match record."""
    return {"white_player_id": p, "black_player_id": opp, "result": "white"}


def _black(p, opp):
    """Helper to build a 'p played black vs opp' match record."""
    return {"white_player_id": opp, "black_player_id": p, "result": "white"}


def _bye(p):
    return {"white_player_id": p, "black_player_id": None, "result": "bye"}


# --- Unit tests -----------------------------------------------------------

def test_streak_basic():
    """Trailing same-color games are counted; the streak STOPS at a different
    color or at the start of history."""
    # No history → no streak.
    assert _color_streak("p1", []) == (None, 0)

    # One game as white → 1-white streak.
    h = [_white("p1", "p2")]
    assert _color_streak("p1", h) == ("white", 1)
    assert _color_streak("p2", h) == ("black", 1)

    # Two whites in a row → 2-white streak.
    h = [_white("p1", "p2"), _white("p1", "p3")]
    assert _color_streak("p1", h) == ("white", 2)

    # WBW → only 1 (the W in front), streak broken by the B.
    h = [_white("p1", "p2"), _black("p1", "p3"), _white("p1", "p4")]
    assert _color_streak("p1", h) == ("white", 1)

    # WWB → 1 black (most recent), the W's don't count.
    h = [_white("p1", "p2"), _white("p1", "p3"), _black("p1", "p4")]
    assert _color_streak("p1", h) == ("black", 1)

    print("  test_streak_basic: OK")


def test_streak_bye_neutral():
    """A bye in the middle of a same-color run does NOT reset the streak.
    This matches FIDE's view that the bye-getter has no color."""
    # W, bye, W → streak of 2 white.
    h = [_white("p1", "p2"), _bye("p1"), _white("p1", "p3")]
    assert _color_streak("p1", h) == ("white", 2), \
        f"expected (white, 2) but got {_color_streak('p1', h)}"

    # W, W, bye, W → streak of 3 white.
    h = [_white("p1", "p2"), _white("p1", "p3"), _bye("p1"), _white("p1", "p4")]
    assert _color_streak("p1", h) == ("white", 3)

    # bye alone → no games, no streak.
    h = [_bye("p1")]
    assert _color_streak("p1", h) == (None, 0)

    print("  test_streak_bye_neutral: OK")


def test_assign_one_streak():
    """One player on a 2-white streak, other not → streaker gets black."""
    p1 = _player("p1")
    p2 = _player("p2")
    # p1 has played W vs p3, W vs p4. p2 played W vs p5, B vs p6.
    h = [
        _white("p1", "p3"), _white("p1", "p4"),
        _white("p2", "p5"), _black("p2", "p6"),
    ]
    # p1 is on a 2-white streak; p2 is on a 1-black streak. p1 should get black.
    w, b = _assign_colors(p1, p2, h)
    assert w == "p2" and b == "p1", \
        f"expected p2 white / p1 black; got {w} white / {b} black"

    # Same logic, swapped argument order — symmetric.
    w, b = _assign_colors(p2, p1, h)
    assert w == "p2" and b == "p1"

    # And the mirror: p1 on 2-black streak → p1 should get white.
    h_mirror = [
        _black("p1", "p3"), _black("p1", "p4"),
        _white("p2", "p5"), _black("p2", "p6"),
    ]
    w, b = _assign_colors(p1, p2, h_mirror)
    assert w == "p1" and b == "p2"

    print("  test_assign_one_streak: OK")


def test_assign_opposite_streaks():
    """Both on 2-streaks, opposite colors → assignment is forced and
    compatible. p1 needs black, p2 needs white."""
    p1 = _player("p1")
    p2 = _player("p2")
    h = [
        _white("p1", "p3"), _white("p1", "p4"),  # p1 on WW
        _black("p2", "p5"), _black("p2", "p6"),  # p2 on BB
    ]
    w, b = _assign_colors(p1, p2, h)
    assert w == "p2" and b == "p1"
    # And the symmetric call.
    w, b = _assign_colors(p2, p1, h)
    assert w == "p2" and b == "p1"
    print("  test_assign_opposite_streaks: OK")


def test_assign_same_streaks_forced():
    """Both on 2-streaks of the SAME color → one of them MUST extend to 3.
    Falls through to cumulative balance: whoever has fewer total whites
    takes the extension; in this case both have equal whites (2 each) and
    blacks (0 each), so it falls to id (lower id gets white).

    The point isn't WHO gets white — it's that the function:
      (a) doesn't crash on an unresolvable streak conflict,
      (b) doesn't loop or stall,
      (c) returns deterministic output.
    """
    p1 = _player("p1")
    p2 = _player("p2")
    h = [
        _white("p1", "p3"), _white("p1", "p4"),  # both on WW
        _white("p2", "p5"), _white("p2", "p6"),
    ]
    # Both have whites=2, blacks=0 → tie chain → lex by id → p1 gets white.
    w, b = _assign_colors(p1, p2, h)
    assert w == "p1" and b == "p2", \
        f"expected p1 white (lex tie); got {w} white / {b} black"
    # Determinism: same answer on the symmetric call.
    w2, b2 = _assign_colors(p2, p1, h)
    assert (w2, b2) == (w, b), "non-deterministic across arg order"
    print("  test_assign_same_streaks_forced: OK")


def test_assign_regression_no_streaks():
    """With no streaks (no prior games), the function must return exactly
    what the pre-task-11 version would. Lock this so future edits don't
    silently change Round 1 behaviour."""
    p1 = _player("p1")
    p2 = _player("p2")
    # No prior games → no streaks → falls through to cumulative balance
    # → falls through to id → p1 (lex smaller) gets white.
    w, b = _assign_colors(p1, p2, [])
    assert w == "p1" and b == "p2"

    # One round each, both played WHITE before (their score is equal,
    # neither on a streak ≥ 2 yet) → cumulative balance: equal whites,
    # equal blacks → falls to id → p1 gets white.
    h = [_white("p1", "p3"), _white("p2", "p4")]
    w, b = _assign_colors(p1, p2, h)
    assert w == "p1" and b == "p2"

    # Now p1 has whites=1, blacks=0; p2 has whites=0, blacks=1.
    # Neither is on a 2-streak. Cumulative: p2 has fewer whites → p2 gets white.
    h = [_white("p1", "p3"), _black("p2", "p4")]
    w, b = _assign_colors(p1, p2, h)
    assert w == "p2" and b == "p1"
    print("  test_assign_regression_no_streaks: OK")


# --- End-to-end test ------------------------------------------------------

def _all_three_streaks_forced(n, rounds, strategy):
    """Run a full tournament; for every 3+ same-color extension that happens,
    verify both players in that pairing were on a same-color 2-streak before
    the round (i.e. the extension was forced, not avoidable). Returns
    (total_extensions, forced_count). Test passes if total == forced."""
    _, matches, _ = simulate(n, rounds, strategy)
    # Walk the matches in round order. To know who was on a 2-streak BEFORE a
    # given round, snapshot history up to the prior round.
    # Group by round_id (insertion order is round-major in simulate()).
    rounds_in_order = []
    seen = {}
    for m in matches:
        rid = m.get("round_id")
        if rid not in seen:
            seen[rid] = []
            rounds_in_order.append(rid)
        seen[rid].append(m)

    total_extensions = 0
    forced_count = 0
    prior_history = []
    for rid in rounds_in_order:
        round_matches = seen[rid]
        for m in round_matches:
            if m.get("result") == "bye":
                continue
            w, b = m["white_player_id"], m["black_player_id"]
            # Look at the streaks of w and b BEFORE this round.
            wc, wl = _color_streak(w, prior_history)
            bc, bl = _color_streak(b, prior_history)
            # Does this game extend a streak to 3+?
            if wc == "white" and wl >= 2:
                total_extensions += 1
                if bc == "white" and bl >= 2:
                    forced_count += 1
            if bc == "black" and bl >= 2:
                total_extensions += 1
                if wc == "black" and wl >= 2:
                    forced_count += 1
        prior_history.extend(round_matches)

    return total_extensions, forced_count


def test_e2e_only_forced_streaks_remain():
    """In a stressful tournament shape, the algorithm must never produce a 3-
    streak that COULD have been avoided. A 3-streak is forced when both
    players in the pairing were already on a 2-streak of the same color —
    one of them HAD to extend, no color-assignment choice could prevent it.

    Important: this doesn't claim 'no 3-streaks ever happen'. The task
    brief acknowledges school clubs won't notice the edge case. The claim
    is the algorithm catches every avoidable case."""
    for n, rounds, strat in [
        (16, 12, "white_wins"),
        (12, 7, "white_wins"),
        (8, 7, "white_wins"),
        (16, 12, "draw"),
    ]:
        total, forced = _all_three_streaks_forced(n, rounds, strat)
        avoidable = total - forced
        print(f"    n={n}, rounds={rounds}, {strat}: "
              f"{total} extensions, {forced} forced, {avoidable} avoidable")
        assert avoidable == 0, \
            f"n={n} {strat}: {avoidable} avoidable 3-streaks (out of {total})"
    print("  test_e2e_only_forced_streaks_remain: OK")


def test_e2e_streak_reduction_vs_baseline():
    """Sanity check: the new code produces fewer total 3+ extensions than
    the pre-task-11 version on a recognised stressful config. This is the
    quantitative case for the task being a net improvement; if a future
    change regresses below this floor, we want to catch it."""
    from app.services import pairing
    # Capture new behavior.
    new_total, _ = _all_three_streaks_forced(16, 12, "white_wins")

    # Temporarily restore the pre-task-11 version.
    real = pairing._assign_colors
    def old_assign(p1, p2, past_matches):
        w1, b1 = pairing._color_counts(p1["id"], past_matches)
        w2, b2 = pairing._color_counts(p2["id"], past_matches)
        if w1 < w2: return p1["id"], p2["id"]
        if w2 < w1: return p2["id"], p1["id"]
        if b1 > b2: return p1["id"], p2["id"]
        if b2 > b1: return p2["id"], p1["id"]
        return (p1["id"], p2["id"]) if p1["id"] < p2["id"] else (p2["id"], p1["id"])
    pairing._assign_colors = old_assign
    try:
        old_total, _ = _all_three_streaks_forced(16, 12, "white_wins")
    finally:
        pairing._assign_colors = real

    print(f"    n=16, 12r, white_wins: old extensions={old_total}, new={new_total}")
    assert new_total < old_total, \
        f"task #11 did not reduce 3+ extensions on the stress config " \
        f"(old={old_total}, new={new_total})"
    print("  test_e2e_streak_reduction_vs_baseline: OK")


# --- Driver ---------------------------------------------------------------

def main():
    print("=== Task #11 tests: color-streak avoidance ===")
    test_streak_basic()
    test_streak_bye_neutral()
    test_assign_one_streak()
    test_assign_opposite_streaks()
    test_assign_same_streaks_forced()
    test_assign_regression_no_streaks()
    test_e2e_only_forced_streaks_remain()
    test_e2e_streak_reduction_vs_baseline()
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())