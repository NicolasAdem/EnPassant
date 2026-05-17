"""For each round with emergency byes, check whether a perfect matching
existed across the full pool (ignoring score-group structure). If yes,
that's a real search miss. If no, the emergency bye was unavoidable given
the player pool's history."""

from itertools import combinations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.pairing import pair_swiss

def perfect_matching_exists(player_ids, played_pairs):
    """Brute-force: is there a perfect matching of player_ids where no
    pair is in played_pairs? Returns matching if so, else None.
    Players list must have even length."""
    n = len(player_ids)
    if n == 0:
        return []
    if n == 1:
        return None  # can't perfectly match an odd count without a bye
    if n % 2 == 1:
        # Try giving the bye to each player in turn.
        for i in range(n):
            rest = player_ids[:i] + player_ids[i+1:]
            sub = perfect_matching_exists(rest, played_pairs)
            if sub is not None:
                return [("BYE", player_ids[i])] + sub
        return None

    # Even count: pair player_ids[0] with each possible partner.
    first = player_ids[0]
    for j in range(1, n):
        partner = player_ids[j]
        if frozenset((first, partner)) in played_pairs:
            continue
        rest = player_ids[1:j] + player_ids[j+1:]
        sub = perfect_matching_exists(rest, played_pairs)
        if sub is not None:
            return [(first, partner)] + sub
    return None


def check(n_players, n_rounds):
    players = [
        {"id": f"p{i:02d}", "name": f"P{i}", "score": 0.0, "elo": 1500 - i*10}
        for i in range(n_players)
    ]
    pby = {p["id"]: p for p in players}
    all_matches = []
    played_pairs = set()
    misses = []

    for r in range(1, n_rounds + 1):
        pairings = pair_swiss(players, all_matches)

        # Did this round emit emergency byes?
        n_byes_this_round = sum(1 for p in pairings if p.get("is_bye"))
        legitimate = 1 if n_players % 2 == 1 else 0
        emergency = n_byes_this_round - legitimate
        if emergency > 0:
            ids = [p["id"] for p in players]
            mm = perfect_matching_exists(ids, played_pairs)
            misses.append({
                "round": r,
                "emergency_byes": emergency,
                "perfect_existed": mm is not None,
                "perfect_pairing": mm if mm else None,
            })

        # Apply (white always wins).
        for p in pairings:
            w, b = p["white_player_id"], p["black_player_id"]
            if b is None:
                pby[w]["score"] += 1.0
                all_matches.append({"white_player_id": w, "black_player_id": None, "result": "bye"})
            else:
                pby[w]["score"] += 1.0
                all_matches.append({"white_player_id": w, "black_player_id": b, "result": "white"})
                played_pairs.add(frozenset((w, b)))

    print(f"\nn={n_players}, rounds={n_rounds}:")
    if not misses:
        print("  No emergency byes.")
        return
    for m in misses:
        verdict = "MISS (perfect pairing existed!)" if m["perfect_existed"] else "unavoidable (no perfect pairing)"
        print(f"  Round {m['round']}: {m['emergency_byes']} emergency bye(s) — {verdict}")
        if m["perfect_existed"]:
            print(f"    Should have been: {m['perfect_pairing']}")


if __name__ == "__main__":
    check(8, 7)
    check(10, 7)
    check(12, 7)
    check(14, 7)
    check(16, 12)