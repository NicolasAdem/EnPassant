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
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,        -- stored lowercased; login identity
    password_hash TEXT NOT NULL,       -- pbkdf2_sha256$iterations$salt$hash
    display_name TEXT NOT NULL,        -- shown in lobbies, prefills the join name
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,            -- random, stored in the ep_session cookie
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,          -- ISO 8601 UTC; checked on every request
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tournaments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host_token TEXT NOT NULL,
    host_user_id TEXT,     -- the account that created/owns this tournament
    status TEXT NOT NULL DEFAULT 'lobby', -- lobby | active | finished
    pairing_mode TEXT NOT NULL DEFAULT 'swiss', -- swiss | random | manual
    current_round INTEGER NOT NULL DEFAULT 0,
    location_mode TEXT NOT NULL DEFAULT 'offsite', -- offsite | onsite (controls table-number UI)
    background_url TEXT,   -- optional image shown softly behind dashboard & projector
    auto_rounds INTEGER NOT NULL DEFAULT 0, -- 1 = auto-start next round when all matches confirmed
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (host_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    name TEXT NOT NULL,
    color TEXT NOT NULL,           -- hex; drives the team chip everywhere
    sort_order INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS players (
    id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    user_id TEXT,          -- the account this player belongs to (one per tournament)
    team_id TEXT,          -- team in team-mode tournaments; NULL otherwise
    name TEXT NOT NULL,
    elo INTEGER NOT NULL DEFAULT 1200,
    score REAL NOT NULL DEFAULT 0, -- 1 win, 0.5 draw, 0 loss
    buchholz REAL NOT NULL DEFAULT 0, -- tiebreak: sum of opponents' scores
    sonneborn_berger REAL NOT NULL DEFAULT 0, -- tiebreak: weighted-by-result opponent scores
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE SET NULL
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
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""
# Indexes on columns that only exist after _migrate() adds them (host_user_id,
# user_id). Kept out of SCHEMA because executescript(SCHEMA) runs BEFORE the
# migration on an existing DB, so referencing those columns there would fail.
POST_MIGRATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_players_user ON players(user_id);
CREATE INDEX IF NOT EXISTS idx_tournaments_host_user ON tournaments(host_user_id);
CREATE INDEX IF NOT EXISTS idx_players_team ON players(team_id);
CREATE INDEX IF NOT EXISTS idx_teams_tournament ON teams(tournament_id);
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
    # Accounts (session 1): tie tournaments to the account that owns them and
    # players to the account that joined. Both are nullable so pre-accounts
    # rows keep working — they simply won't surface in anyone's lobby.
    add_column("tournaments", "host_user_id", "TEXT")
    add_column("players", "user_id", "TEXT")
    # Teams (session 4): a tournament is in "team mode" when it has rows in the
    # teams table; players carry their team_id. Both nullable so non-team and
    # pre-teams tournaments are unaffected.
    add_column("players", "team_id", "TEXT")
    # Personalization (session 5): optional dashboard/projector background image.
    add_column("tournaments", "background_url", "TEXT")
    # Auto-advance (session 6): start the next round automatically once every
    # match in the current round is confirmed.
    add_column("tournaments", "auto_rounds", "INTEGER NOT NULL DEFAULT 0")
    # Task #7: on-site / off-site location mode + per-match table numbers.
    # Existing tournaments default to 'offsite', which means table-number UI
    # stays hidden — preserves the previous behavior exactly.
    add_column("tournaments", "location_mode", "TEXT NOT NULL DEFAULT 'offsite'")
    # table_number is nullable on purpose (no NOT NULL): offsite matches never
    # have one, and SQLite can't add a NOT NULL column without a default on an
    # existing table anyway.
    add_column("matches", "table_number", "INTEGER")
    # Now that host_user_id / user_id are guaranteed to exist, their indexes
    # are safe to create.
    conn.executescript(POST_MIGRATE_INDEXES)


def init_db():
    """Create tables if they don't exist, then run migrations."""
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)