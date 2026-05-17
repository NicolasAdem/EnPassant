"""
Business logic for tournaments. Routers stay thin; this does the work.
"""

import uuid
import json
import secrets
from typing import List, Optional
from ..database import db
from . import pairing


def _short_id() -> str:
    """6-char tournament code, friendly for typing or saying out loud."""
    # Avoid lookalikes: no 0/O, 1/I/L
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def _uuid() -> str:
    return uuid.uuid4().hex


def create_tournament(name: str, pairing_mode: str = "swiss") -> dict:
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
            "INSERT INTO tournaments (id, name, host_token, pairing_mode) VALUES (?, ?, ?, ?)",
            (tid, name, host_token, pairing_mode),
        )
        _log_event(conn, tid, "tournament_created", f"Tournament '{name}' created.")
    return {"id": tid, "name": name, "host_token": host_token, "pairing_mode": pairing_mode}


def get_tournament(tid: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM tournaments WHERE id = ?", (tid,)).fetchone()
        return dict(row) if row else None


def verify_host(tid: str, token: str) -> bool:
    t = get_tournament(tid)
    return t is not None and t["host_token"] == token


def add_player(tid: str, name: str, elo: int = 1200) -> Optional[dict]:
    """Add a player to a tournament. Returns the player or None if tournament missing."""
    t = get_tournament(tid)
    if not t:
        return None
    if t["status"] == "finished":
        return None
    pid = _uuid()
    name = name.strip()[:40]
    if not name:
        return None
    with db() as conn:
        conn.execute(
            "INSERT INTO players (id, tournament_id, name, elo) VALUES (?, ?, ?, ?)",
            (pid, tid, name, elo),
        )
        _log_event(conn, tid, "join", f"{name} joined the tournament.", {"player_id": pid, "name": name})
    return {"id": pid, "tournament_id": tid, "name": name, "elo": elo, "score": 0}


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


def start_next_round(tid: str, mode_override: Optional[str] = None) -> Optional[dict]:
    """Generate pairings for the next round."""
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

    mode = mode_override or t["pairing_mode"]
    past = list_past_matches(tid)
    pairings = pairing.generate_pairings(mode, players, past)

    # If the pairing engine returns nothing, the tournament is logically over
    # (most common with round-robin once the full schedule is exhausted, or
    # manual mode at this stage). Don't silently create an empty round.
    if not pairings and mode != "manual":
        return {"error": "Tournament complete — no more rounds to play."}

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
            else:
                conn.execute(
                    """
                    INSERT INTO matches (id, round_id, tournament_id, board_number,
                                         white_player_id, black_player_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (mid, rid, tid, p["board_number"], p["white_player_id"], p["black_player_id"]),
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
        else:
            conn.execute(
                """INSERT INTO matches (id, round_id, tournament_id, board_number,
                    white_player_id, black_player_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                (mid, rid, tid, board, white_pid, black_pid),
            )
        return {"id": mid, "board_number": board, "round_number": round_num}


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


def _finalize_match(conn, match_id: str, confirmer: str):
    """Internal: mark confirmed, update player scores, log event, recompute tiebreaks."""
    m = dict(conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone())
    result = m["result"]
    white_id = m["white_player_id"]
    black_id = m["black_player_id"]

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


def get_state_snapshot(tid: str) -> dict:
    """Full snapshot used on initial WebSocket connection or full page render."""
    t = get_tournament(tid)
    if not t:
        return {}
    return {
        "tournament": t,
        "players": list_players(tid),
        "current_matches": list_current_round_matches(tid),
        "events": recent_events(tid, 30),
    }