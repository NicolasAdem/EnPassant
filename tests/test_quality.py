"""Verify the algorithm's Swiss-quality output on normal cases.

For round 1 (no history) and round 2 (1 round of history), the pairings
should follow the canonical fold pattern: top half vs bottom half within
each score group. If this is broken, the fallback is being triggered when
it shouldn't be."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.pairing import pair_swiss

def make_players(n, base_elo=2000):
    return [
        {"id": f"p{i:02d}", "name": f"P{i}", "score": 0.0, "elo": base_elo - i * 50}
        for i in range(n)
    ]


# Round 1: 8 players, no history. Canonical fold: p0-p4, p1-p5, p2-p6, p3-p7.
players = make_players(8)
pairings = pair_swiss(players, [])
print("Round 1, n=8:")
for p in pairings:
    print(f"  Board {p['board_number']}: {p['white_player_id']} vs {p['black_player_id']}")

expected_pairs = {frozenset(("p00", "p04")), frozenset(("p01", "p05")),
                  frozenset(("p02", "p06")), frozenset(("p03", "p07"))}
actual_pairs = set()
for p in pairings:
    if not p.get("is_bye"):
        actual_pairs.add(frozenset((p["white_player_id"], p["black_player_id"])))

print(f"\nExpected canonical fold: {expected_pairs == actual_pairs}")
if expected_pairs != actual_pairs:
    print(f"  Expected: {expected_pairs}")
    print(f"  Got:      {actual_pairs}")


# Round 2: after canonical wins, scores are [1,1,1,1,0,0,0,0].
# Score-1 group: p0,p1,p2,p3 → fold to p0-p2, p1-p3.
# Score-0 group: p4,p5,p6,p7 → fold to p4-p6, p5-p7.
players = make_players(8)
players[0]["score"] = 1.0
players[1]["score"] = 1.0
players[2]["score"] = 1.0
players[3]["score"] = 1.0
past = [
    {"white_player_id": "p00", "black_player_id": "p04", "result": "white"},
    {"white_player_id": "p01", "black_player_id": "p05", "result": "white"},
    {"white_player_id": "p02", "black_player_id": "p06", "result": "white"},
    {"white_player_id": "p03", "black_player_id": "p07", "result": "white"},
]
pairings = pair_swiss(players, past)
print("\nRound 2, n=8 (after canonical winners):")
for p in pairings:
    print(f"  Board {p['board_number']}: {p['white_player_id']} vs {p['black_player_id']}")

expected_pairs = {frozenset(("p00", "p02")), frozenset(("p01", "p03")),
                  frozenset(("p04", "p06")), frozenset(("p05", "p07"))}
actual_pairs = set()
for p in pairings:
    if not p.get("is_bye"):
        actual_pairs.add(frozenset((p["white_player_id"], p["black_player_id"])))

print(f"\nExpected Dutch fold within score groups: {expected_pairs == actual_pairs}")
if expected_pairs != actual_pairs:
    print(f"  Expected: {expected_pairs}")
    print(f"  Got:      {actual_pairs}")