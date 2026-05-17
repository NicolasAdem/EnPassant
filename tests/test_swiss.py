"""
Verify task #2: stress-test the existing pair_swiss.

Tests mirror the A/B notes at the bottom of tasks.txt:
- 16 players, 12 rounds, white always wins (worst case for rematches).
- Smaller configs: 6/8/10/12 players across 7 rounds.
- Edge cases: forced-bye cycle (3 and 5 players).
"""

import sys
from collections import defaultdict
from itertools import combinations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.pairing import pair_swiss

def make_players(n, base_elo=1500):
    return [
        {"id": f"p{i:02d}", "name": f"Player{i}", "score": 0.0, "elo": base_elo - i * 10}
        for i in range(n)
    ]


def simulate(n_players, n_rounds, result_strategy="white_wins"):
    """
    Run a full tournament. Returns (players, all_matches, stats).

    result_strategy: 'white_wins' | 'draw' | 'mixed'
    """
    players = make_players(n_players)
    players_by_id = {p["id"]: p for p in players}
    all_matches = []
    rematches = []
    emergency_byes = 0
    legitimate_byes = 0
    missing_player_rounds = []

    for round_num in range(1, n_rounds + 1):
        pairings = pair_swiss(players, all_matches)

        # Sanity check 1: every player appears at most once this round.
        players_this_round = []
        for p in pairings:
            if p["white_player_id"]:
                players_this_round.append(p["white_player_id"])
            if p["black_player_id"]:
                players_this_round.append(p["black_player_id"])
        if len(players_this_round) != len(set(players_this_round)):
            raise AssertionError(
                f"Round {round_num}: player appears twice in pairings: {players_this_round}"
            )

        # Sanity check 2: every active player is paired exactly once.
        missing = set(p["id"] for p in players) - set(players_this_round)
        if missing:
            missing_player_rounds.append((round_num, missing))

        # Check for rematches and count byes.
        for p in pairings:
            w, b = p["white_player_id"], p["black_player_id"]
            if b is None:
                # Bye: distinguish legitimate (odd N) vs emergency (n is even
                # but pairing engine still couldn't pair this player).
                if n_players % 2 == 1:
                    legitimate_byes += 1
                else:
                    emergency_byes += 1
                # Bye gives 1 point.
                players_by_id[w]["score"] += 1.0
            else:
                # Was this pair already played?
                for m in all_matches:
                    if m.get("result") == "bye":
                        continue
                    if {m["white_player_id"], m["black_player_id"]} == {w, b}:
                        rematches.append((round_num, w, b))
                        break
                # Record the match and apply score.
                match_record = {
                    "white_player_id": w,
                    "black_player_id": b,
                    "result": "white" if result_strategy == "white_wins" else "draw",
                    "round_id": f"r{round_num}",
                }
                all_matches.append(match_record)
                if result_strategy == "white_wins":
                    players_by_id[w]["score"] += 1.0
                elif result_strategy == "draw":
                    players_by_id[w]["score"] += 0.5
                    players_by_id[b]["score"] += 0.5

            # Record the bye as a match too (so _had_bye works correctly next round).
            if b is None:
                all_matches.append({
                    "white_player_id": w,
                    "black_player_id": None,
                    "result": "bye",
                    "round_id": f"r{round_num}",
                })

    stats = {
        "rematches": rematches,
        "emergency_byes": emergency_byes,
        "legitimate_byes": legitimate_byes,
        "missing_player_rounds": missing_player_rounds,
        "total_matches": len([m for m in all_matches if m["result"] != "bye"]),
    }
    return players, all_matches, stats


def check_double_byes(all_matches):
    """Return list of (player_id, count) for players with >1 bye."""
    counts = defaultdict(int)
    for m in all_matches:
        if m.get("result") == "bye":
            counts[m["white_player_id"]] += 1
    return [(pid, c) for pid, c in counts.items() if c > 1]


def report(name, n, rounds, strategy, stats, all_matches):
    print(f"\n=== {name} (n={n}, rounds={rounds}, {strategy}) ===")
    print(f"  Total non-bye matches:    {stats['total_matches']}")
    print(f"  Rematches:                {len(stats['rematches'])}")
    print(f"  Legitimate byes (odd N):  {stats['legitimate_byes']}")
    print(f"  Emergency byes:           {stats['emergency_byes']}")
    print(f"  Missing-player rounds:    {len(stats['missing_player_rounds'])}")
    doubles = check_double_byes(all_matches)
    print(f"  Players with >1 bye:      {len(doubles)} {doubles if doubles else ''}")
    if stats["rematches"]:
        for r in stats["rematches"][:5]:
            print(f"    rematch in round {r[0]}: {r[1]} vs {r[2]}")
    return (
        len(stats["rematches"]) == 0
        and len(stats["missing_player_rounds"]) == 0
    )


def main():
    all_passed = True

    # The headline test from tasks.txt notes: 16 players, 12 rounds, white wins.
    players, matches, stats = simulate(16, 12, "white_wins")
    all_passed &= report("Headline (matches tasks.txt A/B notes)", 16, 12, "white_wins", stats, matches)

    # Smaller realistic configs.
    for n in [6, 8, 10, 12, 14]:
        rounds = min(n - 1, 7)  # cap at 7, school-event scale
        players, matches, stats = simulate(n, rounds, "white_wins")
        all_passed &= report(f"n={n}", n, rounds, "white_wins", stats, matches)

    # Odd player counts.
    for n in [5, 7, 9, 11]:
        rounds = n - 1
        players, matches, stats = simulate(n, rounds, "white_wins")
        all_passed &= report(f"odd n={n}", n, rounds, "white_wins", stats, matches)

    # Many rounds on small group: tests rematch avoidance hard.
    # n=8 has C(8,2)=28 possible pairs, n*rounds/2 = 8*7/2 = 28 matches → must
    # be a complete round-robin without rematches if Swiss is working.
    players, matches, stats = simulate(8, 7, "white_wins")
    n_unique_pairs = len(set(frozenset((m["white_player_id"], m["black_player_id"]))
                              for m in matches if m["result"] != "bye"))
    print(f"\n  n=8, 7 rounds: {n_unique_pairs} unique pairs (expected 28 for full RR)")

    # Draws scenario (everyone same score, hardest for groups).
    players, matches, stats = simulate(12, 7, "draw")
    all_passed &= report("All-draws (single huge score group)", 12, 7, "draw", stats, matches)

    # Edge case: 3 players, 5 rounds (forces bye cycling).
    players, matches, stats = simulate(3, 5, "white_wins")
    all_passed &= report("Tiny: n=3, 5 rounds", 3, 5, "white_wins", stats, matches)

    print("\n" + "=" * 50)
    print(f"OVERALL: {'PASSED' if all_passed else 'FAILED'}")
    print("=" * 50)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())