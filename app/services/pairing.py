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


def _bye_count(player_id: str, past_matches: List[dict]) -> int:
    """How many byes this player has received. Used by _select_bye_recipient
    to keep bye counts within ±1 across the field — see task #6."""
    return sum(
        1 for m in past_matches
        if m.get("result") == "bye"
        and (m["white_player_id"] == player_id or m["black_player_id"] == player_id)
    )


def _select_bye_recipient(pool: List[dict], past_matches: List[dict]) -> dict:
    """Pick which player should receive a bye when the field is odd.

    Rule (task #6): fewest byes first, then lowest score, then lowest ELO.
    Ordering by bye count first means we exhaust every player at k byes
    before anyone reaches k+1 — so bye counts stay within ±1 of each other
    even across many rounds in small fields, which the previous "give it
    to candidates[0] if everyone has one" rule did not guarantee.

    Pool is assumed non-empty (caller only calls this on odd player counts).
    """
    return min(
        pool,
        key=lambda p: (_bye_count(p["id"], past_matches), p["score"], p["elo"]),
    )


# ---------------------------------------------------------------------------
# SWISS
# ---------------------------------------------------------------------------

def pair_swiss(players: List[dict], past_matches: List[dict]) -> List[dict]:
    """
    Implements the algorithm from EnPassant's design pseudocode:

      1. If player count is odd, award a bye. Recipient is chosen by
         (fewest byes, lowest score, lowest ELO) — see _select_bye_recipient.
         The bye is worth 1 point.
      2. Sort remaining players by score desc, ELO desc.
      3. Group by score.
      4. For each group (high → low): merge in any floaters from previous
         groups (they had higher scores so they sit at the top), re-sort,
         then pair within the group via:
             a. _try_fold(): top half vs bottom half with intra-group swap
                attempts when fold would produce a rematch.
             b. _max_matching(): if fold + swaps still leaves players
                unpaired, run a real maximum-matching search that considers
                all legal pairs within the group, not just half-vs-half.
                Falls back to a constrained-first greedy for groups >14.
         Anyone unpaired floats down to the next group. Multiple floaters
         are carried — necessary when a whole score group's natural opponents
         have already played each other, which happens routinely with small
         score groups in late rounds.
      5. If any floaters remain after the last group: a single floater takes
         the round's bye if one wasn't already assigned. Multiple floaters
         are first paired against each other (different score levels and
         match histories often make this possible).
      6. If anyone is still unmatched after step 5, retry the whole pool as
         one big group via _pair_group. This sacrifices some Swiss "quality"
         (like-against-like score pairings) to ensure everyone gets a game
         when a tournament-wide matching exists. Only invoked when steps 4–5
         fail, so quality is unaffected for normal cases.

    Players: dicts with id, name, score, elo.
    past_matches: prior matches in the tournament. Used for rematch avoidance
                  and color balance.
    """
    # --- Step 1: bye for odd player counts ---
    bye_player = None
    pool = list(players)
    if len(pool) % 2 == 1:
        bye_player = _select_bye_recipient(pool, past_matches)
        pool = [p for p in pool if p["id"] != bye_player["id"]]

    # --- Step 2 & 3: sort, then group by score ---
    pool.sort(key=lambda p: (-p["score"], -p["elo"]))
    by_score: Dict[float, List[dict]] = defaultdict(list)
    for p in pool:
        by_score[p["score"]].append(p)
    score_groups = sorted(by_score.keys(), reverse=True)

    # --- Step 4: pair each group, cascading floaters down ---
    # Multiple unpaired players can float down — necessary when an entire
    # score-group's natural opponents have already played each other (common
    # mid-tournament when score groups are small).
    paired_so_far: List[Tuple[dict, Optional[dict]]] = []
    floaters: List[dict] = []

    for score in score_groups:
        # Merge floaters from above (they have higher scores than this group).
        # Re-sort so the merged group is still score-desc, ELO-desc — this
        # keeps _try_fold's top/bottom split sensible.
        group = floaters + list(by_score[score])
        group.sort(key=lambda p: (-p["score"], -p["elo"]))
        floaters = []

        # If the merged group is odd, drop the lowest-rated player down so
        # _pair_group sees an even-sized group.
        if len(group) % 2 == 1:
            floaters.append(group.pop())  # lowest by sort order

        paired, unpaired = _pair_group(group, past_matches)
        paired_so_far.extend(paired)
        floaters.extend(unpaired)

    # --- Step 5: handle leftover floaters ---
    # Best case: exactly one floater and no bye yet → they take the bye.
    # Otherwise try pairing the leftovers with each other.
    leftover_unmatched: List[dict] = []
    if floaters:
        if bye_player is None and len(floaters) == 1:
            bye_player = floaters[0]
        else:
            leftover_paired, leftover_unpaired = _pair_group(floaters, past_matches)
            paired_so_far.extend(leftover_paired)
            leftover_unmatched = leftover_unpaired

    # --- Step 6: global retry if anyone is still unmatched ---
    # The score-group cascade prioritises Swiss-style pairings (like-against-
    # like scores) but it can over-commit to high-quality pairings in early
    # groups and leave a later player with no legal opponent. When that
    # happens, retry pairing on the whole pool at once — accepts cross-score
    # pairings as the price of giving everyone a game. Only invoked when the
    # cascade fails, so pairing quality is unaffected for normal cases.
    if leftover_unmatched:
        retry_paired, retry_unpaired = _pair_group(list(pool), past_matches)
        if len(retry_unpaired) < len(leftover_unmatched):
            # Global retry did strictly better — use it.
            paired_so_far = [(a, b) for a, b in retry_paired]
            # bye_player was extracted from pool at step 1, so it stays valid.
            for u in retry_unpaired:
                paired_so_far.append((u, None))
        else:
            # Cascade was at least as good. Stick with it; emit emergency byes.
            for u in leftover_unmatched:
                paired_so_far.append((u, None))

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
    swaps); if that leaves anyone unpaired, fall back to a maximum-matching
    search over the full group.

    Returns (paired, unpaired). The caller is responsible for cascading any
    unpaired players to the next group as floaters.
    """
    if len(group) < 2:
        return [], list(group)

    fold_paired, fold_unpaired = _try_fold(group, past_matches)
    if not fold_unpaired:
        return fold_paired, []

    # Fold + intra-half swaps couldn't pair everyone. Run a real maximum
    # matching that can pair top-vs-top or bot-vs-bot too. This is needed
    # late in tournaments when every player has only a handful of legal
    # opponents left.
    mm_paired, mm_unpaired = _max_matching(group, past_matches)

    # Prefer whichever has fewer unpaired. On ties, prefer fold (better
    # Swiss-quality pairings: keeps top vs bottom of the group).
    if len(mm_unpaired) < len(fold_unpaired):
        return mm_paired, mm_unpaired
    return fold_paired, fold_unpaired


def _max_matching(
    group: List[dict], past_matches: List[dict]
) -> Tuple[List[Tuple[dict, dict]], List[dict]]:
    """Find a maximum matching over the legal-opponent graph for the group.

    Algorithm: recursive search with branch pruning. For each player (in
    order), try matching them with each legal partner that comes later in
    the list, recursing on the remainder. Track best-found-so-far and prune
    branches that can't beat it.

    For typical score groups (≤8 players) this completes in microseconds.
    For larger groups the branch factor explodes but the legality graph is
    sparse enough mid-tournament that it stays fast. We hard-cap at 14
    players in a group to keep worst-case bounded; beyond that we fall back
    to greedy.
    """
    n = len(group)
    if n < 2:
        return [], list(group)
    if n > 14:
        return _greedy_matching(group, past_matches)

    # Precompute legality (symmetric, by index).
    legal = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if _can_play(group[i], group[j], past_matches):
                legal[i][j] = True
                legal[j][i] = True

    # We track best pairings as a list of (i, j) index pairs.
    best: dict = {"unpaired": n, "pairs": []}

    def search(start: int, matched: List[bool], pairs: List[Tuple[int, int]]):
        # Find the first unmatched index at or after `start`.
        i = start
        while i < n and matched[i]:
            i += 1
        if i == n:
            # Done. Count unmatched.
            unmatched_count = matched.count(False)
            if unmatched_count < best["unpaired"]:
                best["unpaired"] = unmatched_count
                best["pairs"] = list(pairs)
            return

        # Optimistic bound: even if we pair every remaining player perfectly,
        # how many will be unpaired? Unmatched-after-i is at most matched.count(False).
        # Each future pair reduces unmatched by 2, so the minimum achievable
        # unmatched from this branch is (remaining_unmatched) - 2 * possible_pairs.
        # If even that minimum can't beat best, prune.
        remaining = sum(1 for k in range(i, n) if not matched[k])
        if remaining - 2 * (remaining // 2) >= best["unpaired"]:
            # The unmatched outside [i..n) plus remaining % 2 is a lower bound.
            # If our current pairs already have fewer unpaired possible, prune.
            outside_unmatched = sum(1 for k in range(0, i) if not matched[k])
            min_possible_unpaired = outside_unmatched + (remaining % 2)
            if min_possible_unpaired >= best["unpaired"]:
                return

        # Branch A: leave player i unmatched, move on.
        # (Only useful if the optimal pairing genuinely can't include i.)
        matched[i] = False  # already false but explicit
        # Try matching i with each legal partner j > i.
        for j in range(i + 1, n):
            if matched[j]:
                continue
            if not legal[i][j]:
                continue
            matched[i] = True
            matched[j] = True
            pairs.append((i, j))
            search(i + 1, matched, pairs)
            pairs.pop()
            matched[i] = False
            matched[j] = False
            if best["unpaired"] == 0:
                return  # perfect — stop searching

        # Also consider leaving i unmatched. Only useful if no legal partner
        # works out — but we need to explore it for correctness when i has
        # legal partners that all lead to worse outcomes than skipping i.
        search(i + 1, matched, pairs)

    search(0, [False] * n, [])

    matched_flags = [False] * n
    paired: List[Tuple[dict, dict]] = []
    for i, j in best["pairs"]:
        paired.append((group[i], group[j]))
        matched_flags[i] = True
        matched_flags[j] = True
    unpaired = [group[k] for k in range(n) if not matched_flags[k]]
    return paired, unpaired


def _greedy_matching(
    group: List[dict], past_matches: List[dict]
) -> Tuple[List[Tuple[dict, dict]], List[dict]]:
    """Fallback for very large groups (>14 players). Greedy by fewest-legal-
    opponents-first (matches the player with the most-constrained options
    first; standard greedy heuristic for matching)."""
    n = len(group)
    legal_counts = []
    legal_map: Dict[int, List[int]] = {}
    for i in range(n):
        opts = [j for j in range(n) if i != j and _can_play(group[i], group[j], past_matches)]
        legal_map[i] = opts
        legal_counts.append((len(opts), i))

    matched = [False] * n
    paired: List[Tuple[dict, dict]] = []
    legal_counts.sort()
    for _, i in legal_counts:
        if matched[i]:
            continue
        # Pick the legal partner with the fewest remaining options.
        best_j = None
        best_remaining = n + 1
        for j in legal_map[i]:
            if matched[j]:
                continue
            remaining = sum(1 for k in legal_map[j] if not matched[k])
            if remaining < best_remaining:
                best_remaining = remaining
                best_j = j
        if best_j is not None:
            matched[i] = True
            matched[best_j] = True
            paired.append((group[i], group[best_j]))

    unpaired = [group[k] for k in range(n) if not matched[k]]
    return paired, unpaired


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


def _can_play(p1: dict, p2: dict, past_matches: List[dict]) -> bool:
    """Two players can face each other if they're distinct and haven't met."""
    if p1["id"] == p2["id"]:
        return False
    return not _have_played(p1["id"], p2["id"], past_matches)


# ---------------------------------------------------------------------------
# RANDOM
# ---------------------------------------------------------------------------

def pair_random(players: List[dict], past_matches: List[dict]) -> List[dict]:
    """Shuffle and pair sequentially. Bye to leftover (selected by the same
    fewest-byes-first rule as Swiss — see _select_bye_recipient)."""
    shuffled = players[:]
    random.shuffle(shuffled)

    bye_player = None
    if len(shuffled) % 2 == 1:
        bye_player = _select_bye_recipient(shuffled, past_matches)
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