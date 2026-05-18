"""
SQLite database layer for EnPassant.

Schema overview:
- tournaments: top-level events
- players: enrolled players per tournament (with score, ELO, ranking)
- rounds: per-round metadata (round number, pairing mode)
- matches: a single board pairing (white player, black player, result, confirmation state)
- events: append-only log used by the live ticker on the projector
"""

import sqlite3
import os
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "enpassant.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    """Context manager that yields a connection and commits on exit."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS tournaments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host_token TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'lobby', -- lobby | active | finished
    pairing_mode TEXT NOT NULL DEFAULT 'swiss', -- swiss | random | manual
    current_round INTEGER NOT NULL DEFAULT 0,
    location_mode TEXT NOT NULL DEFAULT 'offsite', -- offsite | onsite (controls table-number UI)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS players (
    id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    name TEXT NOT NULL,
    elo INTEGER NOT NULL DEFAULT 1200,
    score REAL NOT NULL DEFAULT 0, -- 1 win, 0.5 draw, 0 loss
    buchholz REAL NOT NULL DEFAULT 0, -- tiebreak: sum of opponents' scores
    sonneborn_berger REAL NOT NULL DEFAULT 0, -- tiebreak: weighted-by-result opponent scores
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rounds (
    id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    round_number INTEGER NOT NULL,
    pairing_mode TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    tournament_id TEXT NOT NULL,
    board_number INTEGER NOT NULL,
    table_number INTEGER,  -- physical table the match is played at; NULL for offsite events
    white_player_id TEXT,  -- nullable for bye
    black_player_id TEXT,  -- nullable for bye
    result TEXT,           -- 'white' | 'black' | 'draw' | 'bye'
    reported_by TEXT,      -- player_id who reported the result
    confirmed_by TEXT,     -- player_id who confirmed (or 'host' if host resolved)
    status TEXT NOT NULL DEFAULT 'pending', -- pending | reported | confirmed | disputed | bye
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TEXT,
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id TEXT NOT NULL,
    kind TEXT NOT NULL, -- 'result' | 'join' | 'round_start' | 'dispute' | 'tournament_start' | 'tournament_end'
    message TEXT NOT NULL,
    payload TEXT,       -- json blob
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_players_tournament ON players(tournament_id);
CREATE INDEX IF NOT EXISTS idx_matches_round ON matches(round_id);
CREATE INDEX IF NOT EXISTS idx_matches_tournament ON matches(tournament_id);
CREATE INDEX IF NOT EXISTS idx_events_tournament ON events(tournament_id, id DESC);
"""


def _migrate(conn):
    """Apply idempotent migrations to keep existing dev databases in sync
    with the schema above. Each step checks before changing.

    sqlite's ALTER TABLE ADD COLUMN is fine to run repeatedly only if we
    guard against the "duplicate column" error, since SQLite doesn't have
    an IF NOT EXISTS clause for ADD COLUMN until 3.35 and we want to work
    on older builds too.
    """
    def add_column(table: str, column: str, decl: str):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    add_column("players", "sonneborn_berger", "REAL NOT NULL DEFAULT 0")
    # Task #7: on-site / off-site location mode + per-match table numbers.
    # Existing tournaments default to 'offsite', which means table-number UI
    # stays hidden — preserves the previous behavior exactly.
    add_column("tournaments", "location_mode", "TEXT NOT NULL DEFAULT 'offsite'")
    # table_number is nullable on purpose (no NOT NULL): offsite matches never
    # have one, and SQLite can't add a NOT NULL column without a default on an
    # existing table anyway.
    add_column("matches", "table_number", "INTEGER")


def init_db():
    """Create tables if they don't exist, then run migrations."""
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)