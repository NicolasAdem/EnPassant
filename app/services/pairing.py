"""
Pairing engine. Three modes:

1. swiss: Pair by score group, sorted by score then ELO. Within a group,
   top half plays bottom half (e.g. with 8 in a group: 1v5, 2v6, 3v7, 4v8).
   Avoid rematches when possible. Float odd player down. Bye goes to the
   lowest-scored player who hasn't had one yet.

2. random: Shuffle players and pair sequentially. Bye to leftover.

3. manual: Returns no pairings; the host creates them by hand on the dashboard.

Color assignment for swiss: balance how many whites/blacks each player has had.
Player with fewer whites gets white. Ties broken alphabetically by id.

This is NOT FIDE-strict — it's a clean implementation that works for school /
club events. The user said "Swiss-style" not "FIDE-rated".
"""

import random
from typing import List, Dict, Tuple, Optional
from collections import defaultdict


def _color_counts(player_id: str, past_matches: List[dict]) -> Tuple[int, int]:
    """Return (whites, blacks) the player has played so far."""
    whites = sum(1 for m in past_matches if m["white_player_id"] == player_id)
    blacks = sum(1 for m in past_matches if m["black_player_id"] == player_id)
    return whites, blacks


def _assign_colors(p1: dict, p2: dict, past_matches: List[dict]) -> Tuple[str, str]:
    """Return (white_id, black_id) trying to balance colors."""
    w1, b1 = _color_counts(p1["id"], past_matches)
    w2, b2 = _color_counts(p2["id"], past_matches)
    # Player with fewer whites gets white.
    if w1 < w2:
        return p1["id"], p2["id"]
    if w2 < w1:
        return p2["id"], p1["id"]
    # Tie: player with more blacks gets white
    if b1 > b2:
        return p1["id"], p2["id"]
    if b2 > b1:
        return p2["id"], p1["id"]
    # Still tied: deterministic by id
    return (p1["id"], p2["id"]) if p1["id"] < p2["id"] else (p2["id"], p1["id"])


def _have_played(p1_id: str, p2_id: str, past_matches: List[dict]) -> bool:
    for m in past_matches:
        if (m["white_player_id"] == p1_id and m["black_player_id"] == p2_id) or \
           (m["white_player_id"] == p2_id and m["black_player_id"] == p1_id):
            return True
    return False


def _had_bye(player_id: str, past_matches: List[dict]) -> bool:
    for m in past_matches:
        if m.get("result") == "bye" and \
           (m["white_player_id"] == player_id or m["black_player_id"] == player_id):
            return True
    return False


def pair_swiss(players: List[dict], past_matches: List[dict]) -> List[dict]:
    """
    Players: list of dicts with id, name, score, elo.
    Past_matches: prior matches in this tournament (for rematch avoidance & color balance).
    Returns: list of {white_player_id, black_player_id, board_number} dicts.
             A bye is represented as white_player_id set, black=None, result='bye'.
    """
    # Group by score
    by_score: Dict[float, List[dict]] = defaultdict(list)
    for p in players:
        by_score[p["score"]].append(p)
    # Sort groups descending
    score_groups = sorted(by_score.keys(), reverse=True)

    # Handle bye first if odd number of total players
    bye_player = None
    if len(players) % 2 == 1:
        # Bye to the lowest-score player who hasn't had one
        candidates = sorted(players, key=lambda p: (p["score"], p["elo"]))
        for c in candidates:
            if not _had_bye(c["id"], past_matches):
                bye_player = c
                break
        if bye_player is None:
            # Everyone has had a bye; give to lowest-score
            bye_player = candidates[0]
        # Remove bye player from their score group
        by_score[bye_player["score"]] = [
            p for p in by_score[bye_player["score"]] if p["id"] != bye_player["id"]
        ]

    pairings = []
    board = 1
    floater: Optional[dict] = None

    for score in score_groups:
        group = sorted(by_score[score], key=lambda p: -p["elo"])
        if floater:
            group.insert(0, floater)
            floater = None
        if len(group) % 2 == 1:
            # Float the lowest-elo player down to next group
            floater = group.pop()
        # Top half vs bottom half
        half = len(group) // 2
        top = group[:half]
        bot = group[half:]

        # Try to avoid rematches: for each top player, try their natural opponent
        # then shuffle within bot if rematch
        used_bot = set()
        for t in top:
            opp = None
            # Try natural match first
            for b in bot:
                if b["id"] in used_bot:
                    continue
                if not _have_played(t["id"], b["id"], past_matches):
                    opp = b
                    break
            # Fallback: any unused bot
            if opp is None:
                for b in bot:
                    if b["id"] not in used_bot:
                        opp = b
                        break
            if opp is None:
                continue
            used_bot.add(opp["id"])
            w, bl = _assign_colors(t, opp, past_matches)
            pairings.append({
                "white_player_id": w,
                "black_player_id": bl,
                "board_number": board,
            })
            board += 1

    # If there's still a floater dangling at the end with no group, they get a bye
    if floater and bye_player is None:
        bye_player = floater

    if bye_player:
        pairings.append({
            "white_player_id": bye_player["id"],
            "black_player_id": None,
            "board_number": board,
            "is_bye": True,
        })

    return pairings


def pair_random(players: List[dict], past_matches: List[dict]) -> List[dict]:
    """Shuffle and pair sequentially. Bye to leftover (lowest-score who hasn't had one)."""
    shuffled = players[:]
    random.shuffle(shuffled)

    bye_player = None
    if len(shuffled) % 2 == 1:
        candidates = sorted(shuffled, key=lambda p: p["score"])
        for c in candidates:
            if not _had_bye(c["id"], past_matches):
                bye_player = c
                break
        if bye_player is None:
            bye_player = candidates[0]
        shuffled = [p for p in shuffled if p["id"] != bye_player["id"]]

    pairings = []
    board = 1
    for i in range(0, len(shuffled), 2):
        p1, p2 = shuffled[i], shuffled[i + 1]
        w, bl = _assign_colors(p1, p2, past_matches)
        pairings.append({
            "white_player_id": w,
            "black_player_id": bl,
            "board_number": board,
        })
        board += 1

    if bye_player:
        pairings.append({
            "white_player_id": bye_player["id"],
            "black_player_id": None,
            "board_number": board,
            "is_bye": True,
        })

    return pairings


def generate_pairings(mode: str, players: List[dict], past_matches: List[dict]) -> List[dict]:
    if mode == "swiss":
        return pair_swiss(players, past_matches)
    if mode == "random":
        return pair_random(players, past_matches)
    if mode == "manual":
        return []  # host will create
    raise ValueError(f"Unknown pairing mode: {mode}")


def calculate_buchholz(player_id: str, all_players: List[dict], all_matches: List[dict]) -> float:
    """Sum of scores of opponents the player has faced. Standard chess tiebreak."""
    player_score = {p["id"]: p["score"] for p in all_players}
    opponents = set()
    for m in all_matches:
        if m.get("result") == "bye":
            continue
        if m["white_player_id"] == player_id and m["black_player_id"]:
            opponents.add(m["black_player_id"])
        elif m["black_player_id"] == player_id and m["white_player_id"]:
            opponents.add(m["white_player_id"])
    return sum(player_score.get(opp, 0) for opp in opponents)
