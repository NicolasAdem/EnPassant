"""
Pairing engine. Four modes:

1. swiss: Pair by score group, sorted by score then ELO. Within a group,
   top half plays bottom half (e.g. with 8 in a group: 1v5, 2v6, 3v7, 4v8).
   Avoid rematches when possible. Float odd player down. Bye goes to the
   lowest-scored player who hasn't had one yet.

2. random: Shuffle players and pair sequentially. Bye to leftover.

3. round_robin: Everyone plays everyone exactly once. Schedule is computed
   with the circle method, which produces n-1 rounds for n players (or n
   rounds with one bye per round if n is odd). Pairings for the *current*
   round are returned based on how many rounds have already been played.

4. manual: Returns no pairings; the host creates them by hand on the dashboard.

Color assignment for swiss/random: balance how many whites/blacks each player
has had. Round-robin gets its colors directly from the circle method, which
already balances them.

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


# ---------------------------------------------------------------------------
# SWISS
# ---------------------------------------------------------------------------

def pair_swiss(players: List[dict], past_matches: List[dict]) -> List[dict]:
    """
    Implements the algorithm from EnPassant's design pseudocode:

      1. If player count is odd, award a bye to the lowest-scored player who
         hasn't had one yet. The bye is worth 1 point.
      2. Sort remaining players by score desc, ELO desc.
      3. Group by score.
      4. For each group (high → low): prepend any floater from the previous
         group, then pair within the group via:
             a. _try_fold(): top half vs bottom half with intra-group swap
                attempts when fold would produce a rematch.
             b. _backtrack(): if fold + swaps still leaves players unpaired,
                try permutations of the bottom half to find the pairing with
                the fewest unpaired players.
         Anyone still unpaired becomes a floater to the next group.
      5. If a floater remains at the very end, they get a bye (only valid
         if step 1 didn't already award one; if it did, we can't award two
         so we pair the floater with someone — should not happen in practice
         because step 1 guarantees an even number after the bye is removed).

    Players: dicts with id, name, score, elo.
    past_matches: prior matches in the tournament. Used for rematch avoidance
                  and color balance.
    """
    # --- Step 1: bye for odd player counts ---
    bye_player = None
    pool = list(players)
    if len(pool) % 2 == 1:
        # Lowest score first, then lowest ELO. Filter to those without a bye.
        candidates = sorted(pool, key=lambda p: (p["score"], p["elo"]))
        for c in candidates:
            if not _had_bye(c["id"], past_matches):
                bye_player = c
                break
        if bye_player is None:
            # Everyone has had a bye — give to the lowest-scoring player.
            # (Doesn't perfectly prevent double-byes; tightening is task #6.)
            bye_player = candidates[0]
        pool = [p for p in pool if p["id"] != bye_player["id"]]

    # --- Step 2 & 3: sort, then group by score ---
    pool.sort(key=lambda p: (-p["score"], -p["elo"]))
    by_score: Dict[float, List[dict]] = defaultdict(list)
    for p in pool:
        by_score[p["score"]].append(p)
    score_groups = sorted(by_score.keys(), reverse=True)

    # --- Step 4: pair each group, cascading floaters down ---
    # Second element is Optional[dict]: when it's None, the first element
    # is getting an emergency bye (no legal opponent left, very rare).
    paired_so_far: List[Tuple[dict, Optional[dict]]] = []
    floater: Optional[dict] = None

    for score in score_groups:
        group = list(by_score[score])
        if floater is not None:
            # Floater enters the next group at the top (it has a higher score
            # than the group it's joining), so it's likely to be in `top`.
            group.insert(0, floater)
            floater = None

        # If the group is now odd, the LOWEST player floats down.
        # (Pseudocode: bottom of `top` becomes floater. With our sort that's
        # the last element of the group.)
        local_floater: Optional[dict] = None
        if len(group) % 2 == 1:
            local_floater = group.pop()

        paired, unpaired = _pair_group(group, past_matches)
        paired_so_far.extend(paired)

        # Anyone unpaired floats down too. We can only carry one floater
        # cleanly between groups; if `_pair_group` left more than one
        # unpaired we promote one and award byes to the rest (rare,
        # signals a heavily-conflicted history).
        carry_down = []
        if local_floater is not None:
            carry_down.append(local_floater)
        carry_down.extend(unpaired)
        if carry_down:
            floater = carry_down[0]
            # Any extra unpaired players from this group cannot be paired
            # later (no one else to face them), so they get an emergency
            # bye. This is a graceful degradation, not a normal path.
            for extra in carry_down[1:]:
                paired_so_far.append((extra, None))  # marker: unpaired bye

    # --- Step 5: handle any leftover floater at the end ---
    if floater is not None:
        if bye_player is None:
            bye_player = floater
        else:
            # We already gave a bye this round and we still have a leftover.
            # This is the same emergency-bye path as above.
            paired_so_far.append((floater, None))

    # --- Build the return list (apply color assignment + board numbering) ---
    pairings: List[dict] = []
    board = 1
    for a, b in paired_so_far:
        if b is None:
            pairings.append({
                "white_player_id": a["id"],
                "black_player_id": None,
                "board_number": board,
                "is_bye": True,
            })
        else:
            w, bl = _assign_colors(a, b, past_matches)
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


def _pair_group(
    group: List[dict],
    past_matches: List[dict],
) -> Tuple[List[Tuple[dict, dict]], List[dict]]:
    """Pair an even-sized group. First try the ideal fold (with intra-group
    swaps); if that leaves anyone unpaired, fall back to backtracking over
    permutations of the bottom half.

    Returns (paired, unpaired). The caller is responsible for cascading any
    unpaired players to the next group as floaters.
    """
    if len(group) < 2:
        return [], list(group)

    fold_paired, fold_unpaired = _try_fold(group, past_matches)
    if not fold_unpaired:
        return fold_paired, []

    # Fold + swaps couldn't pair everyone. Try the more expensive backtrack.
    bt_paired, bt_unpaired = _backtrack(group, past_matches)

    # Take whichever attempt got further (fewer unpaired). If tied, prefer
    # the fold result because it preserves ranking-quality matchups.
    if len(bt_unpaired) < len(fold_unpaired):
        return bt_paired, bt_unpaired
    return fold_paired, fold_unpaired


def _try_fold(
    group: List[dict], past_matches: List[dict]
) -> Tuple[List[Tuple[dict, dict]], List[dict]]:
    """Top-half vs bottom-half pairing. On rematch, attempt a single swap
    within the bottom half. Players for whom no clean partner exists become
    unpaired."""
    n = len(group)
    mid = n // 2
    top = group[:mid]
    bot = list(group[mid:])  # mutable copy — we may swap inside it

    paired: List[Tuple[dict, dict]] = []
    unpaired: List[dict] = []
    used_bot: set = set()

    for i, t_player in enumerate(top):
        if i >= len(bot):
            unpaired.append(t_player)
            continue

        natural = bot[i]
        if natural["id"] not in used_bot and _can_play(t_player, natural, past_matches):
            paired.append((t_player, natural))
            used_bot.add(natural["id"])
            continue

        # Natural match is a rematch (or already used). Try to swap natural
        # with another unused player in bot that t_player CAN face.
        swap_idx = _find_swap(t_player, bot, i, past_matches, used_bot)
        if swap_idx is not None:
            bot[i], bot[swap_idx] = bot[swap_idx], bot[i]
            paired.append((t_player, bot[i]))
            used_bot.add(bot[i]["id"])
        else:
            unpaired.append(t_player)

    # Any bottom-half players that didn't get picked are also unpaired —
    # they must cascade as floaters too, otherwise they vanish from the
    # tournament entirely.
    for b_player in bot:
        if b_player["id"] not in used_bot:
            unpaired.append(b_player)

    return paired, unpaired


def _find_swap(
    player: dict,
    bot: List[dict],
    current_index: int,
    past_matches: List[dict],
    used_bot: set,
) -> Optional[int]:
    """Find an index in bot (after current_index) where `player` can face the
    candidate without producing a rematch and the candidate isn't already
    paired."""
    for j in range(current_index + 1, len(bot)):
        candidate = bot[j]
        if candidate["id"] in used_bot:
            continue
        if _can_play(player, candidate, past_matches):
            return j
    return None


def _backtrack(
    group: List[dict], past_matches: List[dict]
) -> Tuple[List[Tuple[dict, dict]], List[dict]]:
    """Last-resort: try permutations of the bottom half to find the assignment
    that pairs the most top players. Bounded — we cap permutations at a sane
    limit so heavily-conflicted groups don't blow up. With ≤8 players in a
    bottom half (i.e. ≤16-player score group, which is already enormous for a
    school event) this fully enumerates."""
    from itertools import permutations

    n = len(group)
    mid = n // 2
    top = group[:mid]
    bot = group[mid:]

    best_paired: List[Tuple[dict, dict]] = []
    best_unpaired: List[dict] = list(top)  # worst case: no one pairs

    # Enumeration limit: 8! = 40320, still cheap. Anything bigger is fine
    # to skip — fold-with-swaps will have handled it well enough.
    if len(bot) > 8:
        return best_paired, best_unpaired

    for perm in permutations(bot):
        paired: List[Tuple[dict, dict]] = []
        unpaired: List[dict] = []
        used_partners: set = set()
        for i, t_player in enumerate(top):
            if i < len(perm) and _can_play(t_player, perm[i], past_matches):
                paired.append((t_player, perm[i]))
                used_partners.add(perm[i]["id"])
            else:
                unpaired.append(t_player)
        # Bottom-half players that didn't get paired are also unpaired
        for b_player in bot:
            if b_player["id"] not in used_partners:
                unpaired.append(b_player)
        if len(unpaired) < len(best_unpaired):
            best_paired, best_unpaired = paired, unpaired
            if not unpaired:
                return best_paired, []  # perfect match — early exit

    return best_paired, best_unpaired


def _can_play(p1: dict, p2: dict, past_matches: List[dict]) -> bool:
    """Two players can face each other if they're distinct and haven't met."""
    if p1["id"] == p2["id"]:
        return False
    return not _have_played(p1["id"], p2["id"], past_matches)


# ---------------------------------------------------------------------------
# RANDOM
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ROUND ROBIN
# ---------------------------------------------------------------------------

def _circle_schedule(player_ids: List[str]) -> List[List[Tuple[Optional[str], Optional[str]]]]:
    """
    Generate a full round-robin schedule using the circle method.

    For n players:
      - If n is even: n-1 rounds, no byes.
      - If n is odd: n rounds, one player gets a bye each round.
        The "ghost" is represented as None — caller must turn (real, None) pairs
        into byes.

    Each pair is returned as (top, bottom) from the circle. Color assignment
    is the caller's job (so it can use the same color-balancing logic as
    swiss / random, which already considers prior rounds in past_matches).

    Algorithm: fix player 0, rotate the others. In each round, pair index i
    with index n-1-i.
    """
    n = len(player_ids)
    if n < 2:
        return []

    # Add a ghost if odd. The player paired with the ghost gets a bye.
    has_ghost = (n % 2 == 1)
    # Type is List[Optional[str]] so we can mix real ids with a None ghost.
    ids: List[Optional[str]] = list(player_ids)
    if has_ghost:
        ids.append(None)
        n += 1

    rounds_count = n - 1
    half = n // 2
    arr = list(ids)

    schedule: List[List[Tuple[Optional[str], Optional[str]]]] = []
    for r in range(rounds_count):
        round_pairs: List[Tuple[Optional[str], Optional[str]]] = []
        for i in range(half):
            round_pairs.append((arr[i], arr[n - 1 - i]))
        schedule.append(round_pairs)
        # Rotate: keep arr[0] fixed, rotate arr[1..n-1] one position.
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]

    return schedule


def pair_round_robin(players: List[dict], past_matches: List[dict]) -> List[dict]:
    """
    Return pairings for the NEXT round of a round-robin.

    Strategy:
      - Sort players by id (stable seeding — must be deterministic across calls,
        otherwise the schedule would shift mid-tournament if a player joined).
      - Generate the full circle-method schedule.
      - Count how many rounds have already been played (= number of distinct
        round_ids among past_matches, but past_matches only carries match rows
        so we infer from a count instead — we just count how many full sets of
        pairings have been recorded and return the next one).
      - Return the pairings for that round.

    If the schedule is exhausted (all players have played each other), return [].
    Caller (tournament service) should treat this as "tournament is over".
    """
    if len(players) < 2:
        return []

    sorted_players = sorted(players, key=lambda p: p["id"])
    player_ids = [p["id"] for p in sorted_players]

    schedule = _circle_schedule(player_ids)
    if not schedule:
        return []

    # Determine which round we're generating.
    # Each completed round of the schedule produces len(schedule[0]) match rows
    # (some of which may be byes when n is odd). We count distinct round_ids
    # if available; otherwise infer from total matches.
    played_round_ids = {m.get("round_id") for m in past_matches if m.get("round_id")}
    rounds_played = len(played_round_ids)

    if rounds_played >= len(schedule):
        # Tournament is logically over; no more pairings.
        return []

    round_pairs = schedule[rounds_played]

    pairings = []
    board = 1
    for top_id, bot_id in round_pairs:
        if top_id is None or bot_id is None:
            # Ghost pairing -> bye for whoever is real
            real = top_id if top_id is not None else bot_id
            pairings.append({
                "white_player_id": real,
                "black_player_id": None,
                "board_number": board,
                "is_bye": True,
            })
        else:
            w, bl = _assign_colors_rr(top_id, bot_id, past_matches)
            pairings.append({
                "white_player_id": w,
                "black_player_id": bl,
                "board_number": board,
            })
        board += 1

    return pairings


def _last_color(player_id: str, past_matches: List[dict]) -> Optional[str]:
    """Return 'white', 'black', or None (bye / no games yet) for the player's
    most recent game. past_matches is assumed in chronological order, which
    matches how the service layer queries (insertion order)."""
    for m in reversed(past_matches):
        if m.get("result") == "bye":
            continue
        if m["white_player_id"] == player_id:
            return "white"
        if m["black_player_id"] == player_id:
            return "black"
    return None


def _assign_colors_rr(p1_id: str, p2_id: str, past_matches: List[dict]) -> Tuple[str, str]:
    """Color assignment specific to round-robin. Prioritises:
      1. Player with fewer whites so far gets white.
      2. Tie → player who played WHITE most recently gets black (and vice
         versa), so colors alternate round-to-round when possible.
      3. Tie → deterministic by id, but with the role flipping each call so
         we don't always favour the lexicographically-smaller id.
    """
    w1, b1 = _color_counts(p1_id, past_matches)
    w2, b2 = _color_counts(p2_id, past_matches)
    if w1 < w2:
        return p1_id, p2_id
    if w2 < w1:
        return p2_id, p1_id
    # Equal whites — check what they played most recently
    last1 = _last_color(p1_id, past_matches)
    last2 = _last_color(p2_id, past_matches)
    if last1 == "white" and last2 != "white":
        return p2_id, p1_id
    if last2 == "white" and last1 != "white":
        return p1_id, p2_id
    if last1 == "black" and last2 != "black":
        return p1_id, p2_id
    if last2 == "black" and last1 != "black":
        return p2_id, p1_id
    # Still tied. Use total past-games count as a salt for the id ordering,
    # so we don't always favour the same player when truly nothing breaks
    # the tie (e.g. round 1 with no history).
    if len(past_matches) % 2 == 0:
        return (p1_id, p2_id) if p1_id < p2_id else (p2_id, p1_id)
    else:
        return (p1_id, p2_id) if p1_id > p2_id else (p2_id, p1_id)


# ---------------------------------------------------------------------------
# DISPATCH
# ---------------------------------------------------------------------------

def generate_pairings(mode: str, players: List[dict], past_matches: List[dict]) -> List[dict]:
    if mode == "swiss":
        return pair_swiss(players, past_matches)
    if mode == "random":
        return pair_random(players, past_matches)
    if mode == "round_robin":
        return pair_round_robin(players, past_matches)
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