"""
Business logic for tournaments. Routers stay thin; this does the work.
"""

import uuid
import json
import re
import secrets
from typing import List, Optional
from ..database import db
from . import pairing


# Distinct, theme-agnostic team colors (readable on both dark and Ivory).
# Index 0..3 map to the first four teams in creation order.
_TEAM_COLORS = ["#5cc8ff", "#ff7a7a", "#b8ff5c", "#c9a0ff"]
MAX_TEAMS = 4


def _short_id() -> str:
    """6-char tournament code, friendly for typing or saying out loud."""
    # Avoid lookalikes: no 0/O, 1/I/L
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def _uuid() -> str:
    return uuid.uuid4().hex


def create_tournament(name: str, pairing_mode: str = "swiss",
                      location_mode: str = "offsite",
                      host_user_id: Optional[str] = None) -> dict:
    """Create a tournament.

    location_mode: 'onsite' (physical event with table numbers) or 'offsite'
    (online / no table UI). Defaults to 'offsite' so callers that don't know
    about the field get the same behavior as before task #7.

    host_user_id: the account that owns this tournament. Set from the logged-in
    user; drives the host's "your tournaments" lobby list.
    """
    tid = _short_id()
    # Ensure uniqueness
    with db() as conn:
        for _ in range(5):
            row = conn.execute("SELECT id FROM tournaments WHERE id = ?", (tid,)).fetchone()
            if not row:
                break
            tid = _short_id()
        host_token = secrets.token_urlsafe(24)
        conn.execute(
            "INSERT INTO tournaments (id, name, host_token, host_user_id, pairing_mode, location_mode) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tid, name, host_token, host_user_id, pairing_mode, location_mode),
        )
        _log_event(conn, tid, "tournament_created", f"Tournament '{name}' created.")
    return {"id": tid, "name": name, "host_token": host_token, "host_user_id": host_user_id,
            "pairing_mode": pairing_mode, "location_mode": location_mode}


def get_tournament(tid: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM tournaments WHERE id = ?", (tid,)).fetchone()
        return dict(row) if row else None


def create_teams(tid: str, names: List[str]) -> List[dict]:
    """Create the tournament's teams from a list of names (2..MAX_TEAMS). Colors
    are auto-assigned from the palette in order. Returns the created teams.
    Names are trimmed; blanks are dropped before counting."""
    clean = [n.strip()[:30] for n in names if n and n.strip()]
    if not (2 <= len(clean) <= MAX_TEAMS):
        return []
    created = []
    with db() as conn:
        for i, name in enumerate(clean):
            tmid = _uuid()
            color = _TEAM_COLORS[i % len(_TEAM_COLORS)]
            conn.execute(
                "INSERT INTO teams (id, tournament_id, name, color, sort_order) VALUES (?, ?, ?, ?, ?)",
                (tmid, tid, name, color, i),
            )
            created.append({"id": tmid, "tournament_id": tid, "name": name, "color": color, "sort_order": i})
    return created


def list_teams(tid: str) -> List[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM teams WHERE tournament_id = ? ORDER BY sort_order ASC",
            (tid,),
        ).fetchall()
        return [dict(r) for r in rows]


def has_teams(tid: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT 1 FROM teams WHERE tournament_id = ? LIMIT 1", (tid,)).fetchone()
        return row is not None


def set_player_team(tid: str, pid: str, team_id: Optional[str]) -> Optional[dict]:
    """Host reassigns a player's team. Lobby only. team_id must belong to this
    tournament (or None to clear). Returns {"ok": True} or {"error": ...}."""
    t = get_tournament(tid)
    if not t:
        return None
    if t["status"] != "lobby":
        return {"error": "Teams can only be changed before the tournament starts."}
    with db() as conn:
        if team_id is not None:
            ok = conn.execute(
                "SELECT 1 FROM teams WHERE id = ? AND tournament_id = ?", (team_id, tid)
            ).fetchone()
            if not ok:
                return {"error": "Unknown team."}
        cur = conn.execute(
            "UPDATE players SET team_id = ? WHERE id = ? AND tournament_id = ?",
            (team_id, pid, tid),
        )
        if cur.rowcount == 0:
            return {"error": "Player not found."}
    return {"ok": True}


# Background image URL: http(s) only, and none of the characters that could
# break out of the CSS url("...") context it ends up in on the client.
_BG_URL_RE = re.compile(r'^https?://[^\s"\'()<>\\]+$')


def set_background(tid: str, url: Optional[str]) -> Optional[dict]:
    """Set or clear the tournament's background image URL. Empty/blank clears it.
    Returns {"background_url": ...}, {"error": ...}, or None if not found."""
    url = (url or "").strip()
    if url == "":
        val = None
    elif _BG_URL_RE.match(url):
        val = url[:500]
    else:
        return {"error": "Enter a valid http(s) image URL."}
    with db() as conn:
        cur = conn.execute("UPDATE tournaments SET background_url = ? WHERE id = ?", (val, tid))
        if cur.rowcount == 0:
            return None
    return {"background_url": val}


def set_background_path(tid: str, path: Optional[str]) -> Optional[dict]:
    """Set background_url to a server-controlled path (e.g. an uploaded file).
    Skips the http(s) validation in set_background because the path is ours."""
    with db() as conn:
        cur = conn.execute("UPDATE tournaments SET background_url = ? WHERE id = ?", (path, tid))
        if cur.rowcount == 0:
            return None
    return {"background_url": path}


def set_auto_rounds(tid: str, enabled: bool) -> Optional[dict]:
    """Toggle automatic round advancement."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE tournaments SET auto_rounds = ? WHERE id = ?", (1 if enabled else 0, tid)
        )
        if cur.rowcount == 0:
            return None
    return {"auto_rounds": 1 if enabled else 0}


def auto_advance_if_ready(tid: str) -> Optional[dict]:
    """If auto-rounds is on and every match in the current round is settled,
    start the next round. Returns the new round's result dict (so the caller can
    broadcast a round_start), or None if nothing advanced.

    Safe to call after any result is finalized — it no-ops unless the round is
    genuinely complete, and start_next_round itself guards the same condition.
    """
    t = get_tournament(tid)
    if not t or not t.get("auto_rounds") or t["status"] != "active":
        return None
    current = list_current_round_matches(tid)
    if not current or any(m["status"] not in ("confirmed", "bye") for m in current):
        return None
    res = start_next_round(tid)
    if res and "error" not in res:
        return res
    return None


def rename_tournament(tid: str, name: str) -> Optional[dict]:
    """Update the tournament's display name. Returns the new name or None if the
    tournament doesn't exist / the name is empty after trimming."""
    name = name.strip()[:60]
    if not name:
        return None
    with db() as conn:
        cur = conn.execute("UPDATE tournaments SET name = ? WHERE id = ?", (name, tid))
        if cur.rowcount == 0:
            return None
    return {"id": tid, "name": name}


def verify_host(tid: str, token: str) -> bool:
    t = get_tournament(tid)
    return t is not None and t["host_token"] == token


def add_player(tid: str, name: str, elo: int = 1200,
               user_id: Optional[str] = None,
               team_id: Optional[str] = None) -> Optional[dict]:
    """Add a player to a tournament. Returns the player, an {"error": ...} dict,
    or None if the tournament is missing/finished.

    A logged-in user gets at most one player per tournament: if they've already
    joined, we return the existing player rather than creating a duplicate.
    New players may only join while the tournament is still in the lobby.

    In team-mode tournaments a valid team_id is required; non-team tournaments
    ignore it.
    """
    t = get_tournament(tid)
    if not t:
        return None
    if t["status"] == "finished":
        return None
    if user_id:
        existing = get_player_for_user(tid, user_id)
        if existing:
            return existing  # idempotent re-join — hand back their existing player
    if t["status"] != "lobby":
        return {"error": "This tournament has already started."}
    name = name.strip()[:40]
    if not name:
        return None

    team_mode = has_teams(tid)
    if team_mode:
        if not team_id:
            return {"error": "Pick a team to join."}
        with db() as conn:
            ok = conn.execute(
                "SELECT 1 FROM teams WHERE id = ? AND tournament_id = ?", (team_id, tid)
            ).fetchone()
        if not ok:
            return {"error": "Unknown team."}
    else:
        team_id = None  # ignore any team on a non-team tournament

    pid = _uuid()
    with db() as conn:
        conn.execute(
            "INSERT INTO players (id, tournament_id, user_id, team_id, name, elo) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, tid, user_id, team_id, name, elo),
        )
        _log_event(conn, tid, "join", f"{name} joined the tournament.", {"player_id": pid, "name": name})
    return {"id": pid, "tournament_id": tid, "user_id": user_id, "team_id": team_id,
            "name": name, "elo": elo, "score": 0}


def get_player_for_user(tid: str, user_id: str) -> Optional[dict]:
    """The player row this account owns in the given tournament, if any."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM players WHERE tournament_id = ? AND user_id = ?",
            (tid, user_id),
        ).fetchone()
        return dict(row) if row else None


def remove_player(tid: str, pid: str) -> bool:
    """Remove a player. Only allowed in lobby."""
    t = get_tournament(tid)
    if not t or t["status"] != "lobby":
        return False
    with db() as conn:
        conn.execute("DELETE FROM players WHERE id = ? AND tournament_id = ?", (pid, tid))
    return True


def list_players(tid: str) -> List[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM players WHERE tournament_id = ? "
            "ORDER BY score DESC, buchholz DESC, sonneborn_berger DESC, elo DESC, name ASC",
            (tid,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_player(pid: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM players WHERE id = ?", (pid,)).fetchone()
        return dict(row) if row else None


def list_past_matches(tid: str) -> List[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM matches WHERE tournament_id = ?", (tid,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_current_round_matches(tid: str) -> List[dict]:
    """Matches in the latest round, with player names joined in."""
    with db() as conn:
        t = conn.execute("SELECT current_round FROM tournaments WHERE id = ?", (tid,)).fetchone()
        if not t or t["current_round"] == 0:
            return []
        round_row = conn.execute(
            "SELECT id FROM rounds WHERE tournament_id = ? AND round_number = ?",
            (tid, t["current_round"]),
        ).fetchone()
        if not round_row:
            return []
        rows = conn.execute(
            """
            SELECT m.*, 
                   pw.name AS white_name, pw.elo AS white_elo,
                   pb.name AS black_name, pb.elo AS black_elo
            FROM matches m
            LEFT JOIN players pw ON pw.id = m.white_player_id
            LEFT JOIN players pb ON pb.id = m.black_player_id
            WHERE m.round_id = ?
            ORDER BY m.board_number ASC
            """,
            (round_row["id"],),
        ).fetchall()
        return [dict(r) for r in rows]


def start_next_round(tid: str) -> Optional[dict]:
    """Generate pairings for the next round.

    The pairing mode is fixed at creation and used for every round — there is
    deliberately no per-round override. A tournament that starts Swiss stays
    Swiss to the end, so standings and tiebreaks remain coherent.
    """
    t = get_tournament(tid)
    if not t:
        return None
    players = list_players(tid)
    if len(players) < 2:
        return None

    # Ensure all current-round matches are confirmed before advancing
    current = list_current_round_matches(tid)
    if current and any(m["status"] not in ("confirmed", "bye") for m in current):
        return {"error": "Previous round has unconfirmed matches."}

    mode = t["pairing_mode"]
    past = list_past_matches(tid)
    pairings = pairing.generate_pairings(mode, players, past)

    # If the pairing engine returns nothing, the tournament is logically over
    # (most common with round-robin once the full schedule is exhausted, or
    # manual mode at this stage). Don't silently create an empty round.
    if not pairings and mode != "manual":
        return {"error": "Tournament complete — no more rounds to play."}

    # Task #7: onsite tournaments get table numbers; offsite ones leave them
    # NULL. We seed table_number from board_number as a sensible default — the
    # host can edit any individual table afterward via /api/.../matches/{mid}/table.
    is_onsite = t.get("location_mode") == "onsite"

    next_round_num = t["current_round"] + 1
    rid = _uuid()
    with db() as conn:
        conn.execute(
            "INSERT INTO rounds (id, tournament_id, round_number, pairing_mode) VALUES (?, ?, ?, ?)",
            (rid, tid, next_round_num, mode),
        )
        conn.execute(
            "UPDATE tournaments SET current_round = ?, status = 'active' WHERE id = ?",
            (next_round_num, tid),
        )
        for p in pairings:
            mid = _uuid()
            is_bye = p.get("is_bye", False)
            if is_bye:
                # Byes have no physical table; table_number stays NULL even onsite.
                conn.execute(
                    """
                    INSERT INTO matches (id, round_id, tournament_id, board_number,
                                         white_player_id, black_player_id, result, status)
                    VALUES (?, ?, ?, ?, ?, NULL, 'bye', 'bye')
                    """,
                    (mid, rid, tid, p["board_number"], p["white_player_id"]),
                )
                # Bye = 1 point
                conn.execute(
                    "UPDATE players SET score = score + 1 WHERE id = ?",
                    (p["white_player_id"],),
                )
                # Task B3: a bye changes the recipient's score, which is an
                # input to every prior opponent's Buchholz/SB. Without this
                # recompute, those opponents carry stale tiebreaks until some
                # later non-bye match of theirs finalizes and re-triggers it
                # via _finalize_match. Same trigger condition as _finalize_match.
                _recompute_tiebreaks(conn, tid)
            else:
                table_num = p["board_number"] if is_onsite else None
                conn.execute(
                    """
                    INSERT INTO matches (id, round_id, tournament_id, board_number, table_number,
                                         white_player_id, black_player_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (mid, rid, tid, p["board_number"], table_num,
                     p["white_player_id"], p["black_player_id"]),
                )
        _log_event(conn, tid, "round_start", f"Round {next_round_num} pairings posted.", {"round": next_round_num, "mode": mode})

    return {"round_number": next_round_num, "pairings": pairings, "mode": mode}


def add_manual_match(tid: str, white_pid: str, black_pid: Optional[str]) -> Optional[dict]:
    """For manual pairing mode: host creates one pairing at a time.
    Adds a match to the current round (or starts round 1 if none active).
    """
    t = get_tournament(tid)
    if not t:
        return None
    is_onsite = t.get("location_mode") == "onsite"
    with db() as conn:
        # Ensure a current round exists in manual mode
        if t["current_round"] == 0:
            rid = _uuid()
            conn.execute(
                "INSERT INTO rounds (id, tournament_id, round_number, pairing_mode) VALUES (?, ?, ?, 'manual')",
                (rid, tid, 1),
            )
            conn.execute(
                "UPDATE tournaments SET current_round = 1, status = 'active' WHERE id = ?",
                (tid,),
            )
            round_num = 1
        else:
            round_num = t["current_round"]
            r = conn.execute(
                "SELECT id FROM rounds WHERE tournament_id = ? AND round_number = ?",
                (tid, round_num),
            ).fetchone()
            rid = r["id"]
        # Determine next board number
        max_board = conn.execute(
            "SELECT COALESCE(MAX(board_number), 0) AS mb FROM matches WHERE round_id = ?",
            (rid,),
        ).fetchone()["mb"]
        board = max_board + 1
        mid = _uuid()
        if black_pid is None:
            conn.execute(
                """INSERT INTO matches (id, round_id, tournament_id, board_number,
                    white_player_id, black_player_id, result, status)
                   VALUES (?, ?, ?, ?, ?, NULL, 'bye', 'bye')""",
                (mid, rid, tid, board, white_pid),
            )
            conn.execute("UPDATE players SET score = score + 1 WHERE id = ?", (white_pid,))
            # Task B3: see _finalize_match / start_next_round. A manual bye
            # changes the recipient's score, so every prior opponent's
            # Buchholz/SB must be refreshed immediately. Without this, the
            # opponent's tiebreaks lag until their own next confirmed match.
            _recompute_tiebreaks(conn, tid)
        else:
            table_num = board if is_onsite else None
            conn.execute(
                """INSERT INTO matches (id, round_id, tournament_id, board_number, table_number,
                    white_player_id, black_player_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (mid, rid, tid, board, table_num, white_pid, black_pid),
            )
        return {"id": mid, "board_number": board, "round_number": round_num}


def set_match_table(tid: str, match_id: str, table_number: Optional[int]) -> Optional[dict]:
    """Set or clear the table_number on a match. Returns the updated row
    (as a dict) or None if the match doesn't exist in the given tournament.

    The API layer is responsible for range-validating table_number; this
    function just writes whatever (int or None) is passed in.
    """
    with db() as conn:
        m = conn.execute(
            "SELECT id, status FROM matches WHERE id = ? AND tournament_id = ?",
            (match_id, tid),
        ).fetchone()
        if not m:
            return None
        # Task 7 invariant: byes never get a table_number. Without this guard
        # a host POST could attach a physical table to a bye row, which then
        # shows up on the projector as a real seat and on the player view as
        # "Find your seat at Table N" for a player who has no opponent.
        if m["status"] == "bye":
            return {"error": "Byes cannot have a table number."}
        conn.execute(
            "UPDATE matches SET table_number = ? WHERE id = ?",
            (table_number, match_id),
        )
        return {"match_id": match_id, "table_number": table_number}


def report_result(match_id: str, reporting_player_id: str, result: str) -> Optional[dict]:
    """Player reports a result. Sets status to 'reported'. Opponent must confirm.
    result: 'white' | 'black' | 'draw'
    """
    if result not in ("white", "black", "draw"):
        return None
    with db() as conn:
        m = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        if not m:
            return None
        m = dict(m)
        if m["status"] in ("confirmed", "bye"):
            return {"error": "Match already finalized."}
        # Task A3: once a match is disputed, the only path forward is the host's
        # /matches/{mid}/resolve endpoint. Without this guard, a re-report would
        # silently reset status back to 'reported' and the next confirm_result()
        # would finalize it — bypassing host arbitration entirely.
        if m["status"] == "disputed":
            return {"error": "Match is disputed — only the host can resolve it."}
        if reporting_player_id not in (m["white_player_id"], m["black_player_id"]):
            return {"error": "Not a participant of this match."}
        conn.execute(
            "UPDATE matches SET result = ?, reported_by = ?, status = 'reported' WHERE id = ?",
            (result, reporting_player_id, match_id),
        )
        return {"match_id": match_id, "result": result, "status": "reported",
                "white_player_id": m["white_player_id"], "black_player_id": m["black_player_id"]}


def confirm_result(match_id: str, confirming_player_id: str, agree: bool) -> Optional[dict]:
    """Opponent confirms or disputes. If agree: finalize. If not: mark disputed."""
    with db() as conn:
        m = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        if not m:
            return None
        m = dict(m)
        if m["status"] != "reported":
            return {"error": "No reported result to confirm."}
        if confirming_player_id not in (m["white_player_id"], m["black_player_id"]):
            return {"error": "Not a participant."}
        if confirming_player_id == m["reported_by"]:
            return {"error": "The reporting player cannot also confirm."}
        if not agree:
            conn.execute(
                "UPDATE matches SET status = 'disputed' WHERE id = ?", (match_id,)
            )
            _log_event(conn, m["tournament_id"], "dispute",
                       f"Result on board {m['board_number']} disputed — host required.",
                       {"match_id": match_id})
            return {"match_id": match_id, "status": "disputed"}
        # Confirmed: finalize and apply scores
        _finalize_match(conn, match_id, confirming_player_id)
        return {"match_id": match_id, "status": "confirmed"}


def host_resolve_match(tid: str, match_id: str, result: str) -> Optional[dict]:
    """Host overrides a disputed match (or directly sets a result)."""
    if result not in ("white", "black", "draw"):
        return None
    with db() as conn:
        m = conn.execute(
            "SELECT * FROM matches WHERE id = ? AND tournament_id = ?",
            (match_id, tid),
        ).fetchone()
        if not m:
            return None
        m = dict(m)
        if m["status"] == "confirmed":
            return {"error": "Already confirmed."}
        # If already had a previous result applied (it wouldn't be, but defensive), don't double-apply
        conn.execute(
            "UPDATE matches SET result = ?, status = 'reported', reported_by = NULL WHERE id = ?",
            (result, match_id),
        )
        _finalize_match(conn, match_id, "host")
        return {"match_id": match_id, "status": "confirmed", "result": result}


def _current_ranks(conn, tid: str) -> dict:
    """Return {player_id: rank} for the tournament, using the canonical standings
    sort key (the same ORDER BY as list_players). Used by _finalize_match to
    compute rank-change deltas for the ticker.

    Re-running the SQL sort instead of re-implementing it in Python guarantees
    the ticker copy ("climbed from #7 to #3") agrees with what the projector
    leaderboard actually shows. Ties are broken by SQLite's ordering of the
    final key (name ASC), so each player gets a unique rank — same as the
    leaderboard.
    """
    rows = conn.execute(
        "SELECT id FROM players WHERE tournament_id = ? "
        "ORDER BY score DESC, buchholz DESC, sonneborn_berger DESC, elo DESC, name ASC",
        (tid,),
    ).fetchall()
    return {r["id"]: i + 1 for i, r in enumerate(rows)}


def _finalize_match(conn, match_id: str, confirmer: str):
    """Internal: mark confirmed, update player scores, log event, recompute tiebreaks."""
    m = dict(conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone())
    result = m["result"]
    white_id = m["white_player_id"]
    black_id = m["black_player_id"]

    # Task #10: snapshot pre-result ranks so we can emit "climbed from #X to #Y"
    # ticker copy. Taken BEFORE we apply scores — this is the ranking the room
    # is currently looking at on the projector. The post-result snapshot below
    # is taken after _recompute_tiebreaks so it agrees with the leaderboard
    # the projector is about to re-render.
    ranks_before = _current_ranks(conn, m["tournament_id"])

    # Apply scores
    if result == "white":
        conn.execute("UPDATE players SET score = score + 1 WHERE id = ?", (white_id,))
    elif result == "black":
        conn.execute("UPDATE players SET score = score + 1 WHERE id = ?", (black_id,))
    elif result == "draw":
        conn.execute("UPDATE players SET score = score + 0.5 WHERE id = ?", (white_id,))
        conn.execute("UPDATE players SET score = score + 0.5 WHERE id = ?", (black_id,))

    conn.execute(
        "UPDATE matches SET status = 'confirmed', confirmed_by = ?, confirmed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (confirmer, match_id),
    )

    # Recompute tiebreaks for all players (cheap; fewer than a few hundred typically)
    _recompute_tiebreaks(conn, m["tournament_id"])

    # Build the event message + payload for the ticker
    wname_row = conn.execute("SELECT name FROM players WHERE id = ?", (white_id,)).fetchone()
    bname_row = conn.execute("SELECT name FROM players WHERE id = ?", (black_id,)).fetchone() if black_id else None
    wname = wname_row["name"] if wname_row else "?"
    bname = bname_row["name"] if bname_row else "?"
    if result == "white":
        msg = f"{wname} beat {bname}"
    elif result == "black":
        msg = f"{bname} beat {wname}"
    else:
        msg = f"{wname} drew {bname}"
    _log_event(conn, m["tournament_id"], "result", msg,
               {"match_id": match_id, "result": result,
                "white": wname, "black": bname, "white_id": white_id, "black_id": black_id})

    # Task #10: emit rank_change events for players whose standing actually
    # moved. We check both participants (the only two players whose ranks can
    # change from this result, since scores propagate to buchholz/SB only via
    # opponent scores which are unaffected by THIS match's confirmation).
    # Wait — that's wrong: _recompute_tiebreaks rebuilds buchholz/SB for every
    # player from opponent scores, and an opponent's score CHANGED, so a third
    # player who has previously played white or black can also have their
    # tiebreak (and therefore rank) shift. We compare the full rank map and
    # emit a rank_change for anyone who moved, capped to keep the ticker
    # readable in pathological cases.
    ranks_after = _current_ranks(conn, m["tournament_id"])
    name_lookup = {white_id: wname}
    if black_id:
        name_lookup[black_id] = bname
    # Emit deltas. Sort by largest absolute movement first; cap at 4 to avoid
    # flooding the ticker if a result reshuffles a crowded field via tiebreaks.
    deltas = []
    for pid, after in ranks_after.items():
        before = ranks_before.get(pid)
        if before is None or before == after:
            continue
        deltas.append((pid, before, after, abs(after - before)))
    deltas.sort(key=lambda d: -d[3])
    for pid, before, after, _mag in deltas[:4]:
        # Look up the name if we haven't already (third-party players whose
        # ranks shifted via tiebreak recompute won't be in name_lookup yet).
        if pid not in name_lookup:
            r = conn.execute("SELECT name FROM players WHERE id = ?", (pid,)).fetchone()
            if not r:
                continue
            name_lookup[pid] = r["name"]
        pname = name_lookup[pid]
        if after < before:
            direction = "up"
            rank_msg = f"{pname} climbed from #{before} to #{after}"
        else:
            direction = "down"
            rank_msg = f"{pname} dropped from #{before} to #{after}"
        _log_event(conn, m["tournament_id"], "rank_change", rank_msg,
                   {"player_id": pid, "name": pname,
                    "from_rank": before, "to_rank": after,
                    "direction": direction})


def _recompute_tiebreaks(conn, tid: str):
    """Recalculate Buchholz and Sonneborn-Berger tiebreaks for all players.

    Buchholz: sum of opponents' final scores. Each distinct opponent counts
    once even if you played them multiple times (matches Swiss convention;
    relevant only with rematches, which the pairing engine avoids anyway).

    Sonneborn-Berger: sum of (opponent_score * game_weight), summed across
    every individual game (so a rematch contributes twice). game_weight is
    1.0 for a win, 0.5 for a draw, 0.0 for a loss. Byes are excluded from
    both (FIDE standard — a bye opponent has no score).
    """
    players = [dict(r) for r in conn.execute(
        "SELECT id, score FROM players WHERE tournament_id = ?", (tid,)
    ).fetchall()]
    matches = [dict(r) for r in conn.execute(
        "SELECT white_player_id, black_player_id, result FROM matches "
        "WHERE tournament_id = ? AND status = 'confirmed'",
        (tid,),
    ).fetchall()]
    score_map = {p["id"]: p["score"] for p in players}

    for p in players:
        opps_for_buchholz = set()
        sb = 0.0
        for m in matches:
            if m["result"] == "bye":
                continue
            w = m["white_player_id"]
            b = m["black_player_id"]
            if w == p["id"] and b:
                opp = b
                if m["result"] == "white":
                    weight = 1.0
                elif m["result"] == "draw":
                    weight = 0.5
                else:
                    weight = 0.0
                opps_for_buchholz.add(opp)
                sb += weight * score_map.get(opp, 0)
            elif b == p["id"] and w:
                opp = w
                if m["result"] == "black":
                    weight = 1.0
                elif m["result"] == "draw":
                    weight = 0.5
                else:
                    weight = 0.0
                opps_for_buchholz.add(opp)
                sb += weight * score_map.get(opp, 0)
        bh = sum(score_map.get(o, 0) for o in opps_for_buchholz)
        conn.execute(
            "UPDATE players SET buchholz = ?, sonneborn_berger = ? WHERE id = ?",
            (bh, sb, p["id"]),
        )


def _log_event(conn, tid: str, kind: str, message: str, payload: Optional[dict] = None):
    conn.execute(
        "INSERT INTO events (tournament_id, kind, message, payload) VALUES (?, ?, ?, ?)",
        (tid, kind, message, json.dumps(payload) if payload else None),
    )


def recent_events(tid: str, limit: int = 20) -> List[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE tournament_id = ? ORDER BY id DESC LIMIT ?",
            (tid, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def pending_confirmations_for_player(pid: str) -> List[dict]:
    """Matches awaiting this player's confirmation (opponent reported, this player hasn't acted)."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT m.*,
                   pw.name AS white_name, pb.name AS black_name,
                   t.name AS tournament_name
            FROM matches m
            LEFT JOIN players pw ON pw.id = m.white_player_id
            LEFT JOIN players pb ON pb.id = m.black_player_id
            JOIN tournaments t ON t.id = m.tournament_id
            WHERE m.status = 'reported'
              AND m.reported_by != ?
              AND (m.white_player_id = ? OR m.black_player_id = ?)
            """,
            (pid, pid, pid),
        ).fetchall()
        return [dict(r) for r in rows]


def matches_for_player(pid: str, tid: str) -> List[dict]:
    """All matches in current round involving this player."""
    with db() as conn:
        t = conn.execute("SELECT current_round FROM tournaments WHERE id = ?", (tid,)).fetchone()
        if not t or t["current_round"] == 0:
            return []
        rows = conn.execute(
            """
            SELECT m.*,
                   pw.name AS white_name, pb.name AS black_name
            FROM matches m
            LEFT JOIN players pw ON pw.id = m.white_player_id
            LEFT JOIN players pb ON pb.id = m.black_player_id
            JOIN rounds r ON r.id = m.round_id
            WHERE m.tournament_id = ?
              AND r.round_number = ?
              AND (m.white_player_id = ? OR m.black_player_id = ?)
            """,
            (tid, t["current_round"], pid, pid),
        ).fetchall()
        return [dict(r) for r in rows]


def end_tournament(tid: str) -> bool:
    with db() as conn:
        conn.execute("UPDATE tournaments SET status = 'finished' WHERE id = ?", (tid,))
        _log_event(conn, tid, "tournament_end", "Tournament finished.")
    return True


def lobbies_for_user(user_id: str) -> dict:
    """Everything the lobby home needs for one account: tournaments they host
    and tournaments they play in, each with a live "what's happening" summary.

    Returns {"hosting": [...], "playing": [...]}. Both lists are newest-first.
    """
    with db() as conn:
        hosting_rows = conn.execute(
            "SELECT * FROM tournaments WHERE host_user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        playing_rows = conn.execute(
            "SELECT t.*, p.id AS player_id FROM tournaments t "
            "JOIN players p ON p.tournament_id = t.id "
            "WHERE p.user_id = ? ORDER BY t.created_at DESC",
            (user_id,),
        ).fetchall()

        hosting = []
        for r in hosting_rows:
            t = dict(r)
            counts = conn.execute(
                "SELECT COUNT(*) AS n FROM players WHERE tournament_id = ?", (t["id"],)
            ).fetchone()
            player_count = counts["n"]
            pending = 0
            disputes = 0
            if t["current_round"]:
                stats = conn.execute(
                    """SELECT
                         SUM(CASE WHEN status NOT IN ('confirmed', 'bye') THEN 1 ELSE 0 END) AS pending,
                         SUM(CASE WHEN status = 'disputed' THEN 1 ELSE 0 END) AS disputes
                       FROM matches m
                       JOIN rounds rd ON rd.id = m.round_id
                       WHERE m.tournament_id = ? AND rd.round_number = ?""",
                    (t["id"], t["current_round"]),
                ).fetchone()
                pending = stats["pending"] or 0
                disputes = stats["disputes"] or 0
            hosting.append({
                "id": t["id"], "name": t["name"], "status": t["status"],
                "pairing_mode": t["pairing_mode"], "current_round": t["current_round"],
                "host_token": t["host_token"], "player_count": player_count,
                "pending_matches": pending, "disputes": disputes,
            })

        playing = []
        for r in playing_rows:
            t = dict(r)
            playing.append({
                "id": t["id"], "name": t["name"], "status": t["status"],
                "current_round": t["current_round"], "player_id": t["player_id"],
                **_player_lobby_status(conn, t, t["player_id"]),
            })

    return {"hosting": hosting, "playing": playing}


def _player_lobby_status(conn, t: dict, pid: str) -> dict:
    """Summarize a player's current situation for the lobby card. Returns
    {"summary": str, "state": str} where state is one of:
    waiting_start | ready | waiting_confirm | needs_confirm | disputed |
    bye | done_round | finished.
    """
    if t["status"] == "finished":
        return {"summary": "Finished", "state": "finished"}
    if t["status"] == "lobby" or not t["current_round"]:
        return {"summary": "Waiting for the host to start", "state": "waiting_start"}

    row = conn.execute(
        """SELECT m.status, m.result, m.reported_by,
                  m.white_player_id, m.black_player_id,
                  pw.name AS white_name, pb.name AS black_name
           FROM matches m
           JOIN rounds rd ON rd.id = m.round_id
           LEFT JOIN players pw ON pw.id = m.white_player_id
           LEFT JOIN players pb ON pb.id = m.black_player_id
           WHERE m.tournament_id = ? AND rd.round_number = ?
             AND (m.white_player_id = ? OR m.black_player_id = ?)""",
        (t["id"], t["current_round"], pid, pid),
    ).fetchone()

    if not row:
        return {"summary": f"Waiting for round {t['current_round'] + 1} pairings", "state": "waiting_start"}
    m = dict(row)
    if m["status"] == "bye":
        return {"summary": "You have a bye this round", "state": "bye"}
    opp = m["black_name"] if m["white_player_id"] == pid else m["white_name"]
    opp = opp or "your opponent"
    if m["status"] == "pending":
        return {"summary": f"Your match is ready — vs {opp}", "state": "ready"}
    if m["status"] == "reported":
        if m["reported_by"] == pid:
            return {"summary": f"Waiting for {opp} to confirm", "state": "waiting_confirm"}
        return {"summary": f"Confirm your result vs {opp}", "state": "needs_confirm"}
    if m["status"] == "disputed":
        return {"summary": "Disputed — the host will resolve it", "state": "disputed"}
    # confirmed
    return {"summary": "Done — waiting for the next round", "state": "done_round"}


def get_state_snapshot(tid: str) -> dict:
    """Full snapshot used on initial WebSocket connection or full page render.

    The host_token is stripped here so every consumer of this function — HTTP
    state endpoint, initial WS snapshot, and broadcast frames — is safe by
    default. The host carries its token in the URL (/host/{tid}?token=...),
    not via state, so nothing downstream needs it.
    """
    t = get_tournament(tid)
    if not t:
        return {}
    t.pop("host_token", None)
    # Task #8: the projector round timer needs a server-side anchor so it shows
    # the same elapsed time to every viewer and survives reconnects. The rounds
    # table already records created_at on insert (SQLite default), so we just
    # surface it here. SQLite's CURRENT_TIMESTAMP returns "YYYY-MM-DD HH:MM:SS"
    # in UTC with a space separator; the JS spec only guarantees Date.parse on
    # the T-separated ISO 8601 variant, so we normalize the space to T and add
    # the Z UTC marker before sending it over the wire.
    round_started_at = None
    if t["current_round"]:
        with db() as conn:
            row = conn.execute(
                "SELECT created_at FROM rounds WHERE tournament_id = ? AND round_number = ?",
                (tid, t["current_round"]),
            ).fetchone()
            if row and row["created_at"]:
                round_started_at = row["created_at"].replace(" ", "T") + "Z"
    return {
        "tournament": t,
        "players": list_players(tid),
        "teams": list_teams(tid),
        "current_matches": list_current_round_matches(tid),
        "events": recent_events(tid, 30),
        "round_started_at": round_started_at,
    }