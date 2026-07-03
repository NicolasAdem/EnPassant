# EnPassant — Codebase Guide

A map of every file in the repo, what it does, and how the pieces connect.

> **Since the original build**, EnPassant grew accounts and a few big features. The
> sections below still describe the core engine accurately; this note summarizes
> what was layered on top:
>
> - **Accounts (email + password).** New `users` and `sessions` tables; `app/services/auth.py`
>   (PBKDF2 hashing, sessions) and `app/routers/auth.py` (`/api/auth/signup|login|logout|me`,
>   `ep_session` cookie). Creating a tournament and joining now require a signed-in user;
>   `tournaments.host_user_id` and `players.user_id` link rows to accounts. The `/` route is
>   a **lobby home** (`home.html`) listing what you host and play in; `login.html` handles both
>   login and signup. The projector view stays public (spectator link).
> - **Locked pairing mode.** The per-round `mode_override` was removed — a tournament uses its
>   creation-time `pairing_mode` for every round.
> - **Teams.** A `teams` table + `players.team_id`. Same-team players can't be paired (enforced
>   in `pairing._can_play`, so Swiss avoids teammates for free); team score = sum of members.
>   Endpoints: teams on create, `team_id` on join, `POST /players/{pid}/team` to reassign.
> - **Settings & personalization.** The host dashboard has a settings drawer: rename
>   (`POST /tournaments/{tid}/name`), theme picker, and a background image
>   (`tournaments.background_url`, `POST /tournaments/{tid}/background`, validated http(s) URL).
> - **Tests.** `test_accounts.py`, `test_mode_lock.py`, `test_teams.py`, and `tests/conftest.py`
>   (a fresh-user auth override so the older engine tests keep passing) were added.

---

## Repository layout

```
EnPassant/
├── app/
│   ├── main.py                  # FastAPI app entry point + WebSocket endpoint
│   ├── database.py              # SQLite schema, connection helpers, migrations
│   ├── websocket_manager.py     # Pub/sub manager for live broadcasts
│   ├── routers/
│   │   ├── api.py               # REST API endpoints (/api/...)
│   │   ├── auth.py              # Signup/login/logout/me + shared auth deps
│   │   └── pages.py             # HTML page routes (/, /host, /player, …)
│   └── services/
│       ├── auth.py              # Users, sessions, PBKDF2 password hashing
│       ├── tournament.py        # Core business logic (players, rounds, teams, scoring)
│       └── pairing.py           # Pairing engine (Swiss, random, round-robin)
├── templates/
│   ├── base.html                # Shared HTML shell (CSS/JS links, theme bootstrap)
│   ├── login.html               # Combined login / signup page
│   ├── home.html                # Lobby home — the user's hosting/playing tournaments
│   ├── index.html               # Legacy landing page (no longer routed)
│   ├── new.html                 # Create-tournament form (mode, location, teams)
│   ├── host.html                # Host dashboard (control panel + settings drawer)
│   ├── join.html                # Player join page (name + team pick)
│   ├── player.html              # Individual player view (report/confirm results)
│   ├── projector.html           # Projector/TV display (leaderboard + boards)
│   └── locked.html              # 403 page shown when host token is missing
├── static/
│   ├── css/
│   │   ├── main.css             # Global styles (dark theme, green accents)
│   │   └── projector.css        # Styles specific to the projector view
│   ├── js/
│   │   └── common.js            # Shared JS utilities (fetch wrapper, WebSocket, toasts)
│   └── img/
│       ├── favicon.svg
│       ├── logo.svg
│       ├── hero-board.jpg
│       ├── pattern-knight.svg
│       └── qr-illustration.svg
├── tests/
│   ├── test_swiss.py            # Stress-tests for Swiss pairing correctness
│   ├── test_tiebreaks.py        # Unit tests for Buchholz + Sonneborn-Berger
│   ├── test_color_streaks.py    # Unit tests for color-balance / streak avoidance
│   ├── test_quality.py          # Pairing quality metrics across many simulations
│   └── check_optimality.py      # Checks that the matching engine finds the maximum
├── requirements.txt             # Python dependencies
├── enpassant.db                 # SQLite database file (created on first run)
└── README.md                    # Quick-start and feature overview
```

---

## File-by-file reference

### `app/main.py`

The FastAPI application object. Responsibilities:

- Instantiates `FastAPI` and mounts the `static/` directory.
- Calls `init_db()` at import time so tables exist before any request arrives.
- Registers the two routers (`pages` and `api`).
- Owns the single WebSocket endpoint at `/ws/{tid}`. On connection it validates the tournament exists, registers the socket with `ConnectionManager`, immediately sends a full state snapshot, then loops waiting for incoming text (client→server messages are not used; the loop just keeps the connection alive).

### `app/database.py`

All database plumbing lives here.

**Schema** (five tables):

| Table | Purpose |
|---|---|
| `tournaments` | One row per event. Holds name, host token, status (`lobby/active/finished`), pairing mode, location mode, and current round number. |
| `players` | One row per participant. Tracks score, Buchholz, and Sonneborn-Berger tiebreaks. |
| `rounds` | One row per round, linking to its tournament. Stores the pairing mode used for that round. |
| `matches` | One row per board. Stores white/black player IDs, result, who reported, who confirmed, status (`pending/reported/confirmed/disputed/bye`), and optional physical table number. |
| `events` | Append-only event log. Powers the live ticker on the projector (kind values: `join`, `result`, `round_start`, `rank_change`, `dispute`, `tournament_end`). |

**Key helpers:**

- `db()` — context manager that opens a connection, yields it, commits on success, rolls back on exception, and closes on exit.
- `init_db()` — runs `CREATE TABLE IF NOT EXISTS` for all tables, then calls `_migrate()`.
- `_migrate()` — idempotent `ALTER TABLE ADD COLUMN` steps for schema evolution. Safe to run against existing databases.

### `app/websocket_manager.py`

`ConnectionManager` holds a `dict[tournament_id → set[WebSocket]]`. Three async methods:

- `connect(tid, ws)` — accepts the socket and adds it to the set.
- `disconnect(tid, ws)` — removes it, deletes the key when the set becomes empty.
- `broadcast(tid, message)` — serialises `message` to JSON and sends it to every socket in the set, pruning dead connections silently.

A module-level singleton `manager` is imported by both `main.py` and `api.py`.

### `app/routers/api.py`

REST API, all under the `/api` prefix. Every mutating endpoint follows the same pattern: validate → call the service layer → broadcast the new state over WebSocket → return the result.

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/tournaments` | Create a tournament; returns `id` and `host_token`. |
| `GET` | `/api/tournaments/{tid}` | Fetch tournament metadata (host token stripped). |
| `GET` | `/api/tournaments/{tid}/state` | Full snapshot (tournament + players + matches + events). |
| `POST` | `/api/tournaments/{tid}/players` | Add a player; broadcasts `join` event. |
| `DELETE` | `/api/tournaments/{tid}/players/{pid}` | Remove a player (lobby only, host only). |
| `POST` | `/api/tournaments/{tid}/rounds` | Advance to the next round; runs the pairing engine. |
| `POST` | `/api/tournaments/{tid}/matches/manual` | Host manually creates a single pairing (manual mode). |
| `POST` | `/api/tournaments/{tid}/matches/{mid}/report` | Player reports a result. |
| `POST` | `/api/tournaments/{tid}/matches/{mid}/confirm` | Opponent confirms or disputes. |
| `POST` | `/api/tournaments/{tid}/matches/{mid}/resolve` | Host overrides a disputed result. |
| `POST` | `/api/tournaments/{tid}/matches/{mid}/table` | Set/clear the physical table number (onsite only). |
| `POST` | `/api/tournaments/{tid}/end` | Mark the tournament finished. |
| `GET` | `/api/players/{pid}/pending` | Matches awaiting this player's confirmation. |
| `GET` | `/api/tournaments/{tid}/players/{pid}/matches` | This player's matches in the current round. |
| `GET` | `/api/tournaments/{tid}/qrcode.png` | Generates and streams a QR code PNG pointing to the join URL. |

Host-only endpoints check the `host_token` query parameter via `_require_host()`.

### `app/routers/pages.py`

Serves Jinja2 HTML templates. No business logic — just validates that the requested tournament/player exists and passes minimal context (IDs, names) to the template.

| Route | Template | Who uses it |
|---|---|---|
| `GET /` | `index.html` | Everyone — landing page |
| `GET /new` | `new.html` | Host creates a tournament |
| `GET /host/{tid}?token=…` | `host.html` | Host control panel (403 → `locked.html` without token) |
| `GET /join/{tid}` | `join.html` | Players arrive here via QR code |
| `GET /player/{tid}/{pid}` | `player.html` | Player's personal view |
| `GET /projector/{tid}` | `projector.html` | TV/projector display |

### `app/services/tournament.py`

All business logic. The routers are intentionally thin; this module does the work.

Key functions:

- **`create_tournament`** — generates a 6-character ID (unambiguous alphabet, no 0/O/1/I/L), creates a cryptographically random `host_token`, inserts the row.
- **`start_next_round`** — guards that all current-round matches are confirmed, delegates to `pairing.generate_pairings`, inserts `rounds` and `matches` rows, awards bye points immediately.
- **`report_result / confirm_result / host_resolve_match`** — implement the three-step result flow (report → confirm/dispute → optional host override). Confirmed results call `_finalize_match`.
- **`_finalize_match`** — applies score deltas, calls `_recompute_tiebreaks`, logs a `result` event, then computes `ranks_before`/`ranks_after` to emit `rank_change` events for anyone who moved in the standings.
- **`_recompute_tiebreaks`** — recalculates Buchholz (sum of unique opponents' scores) and Sonneborn-Berger (opponent score × game weight, summed across every game) for all players in one pass.
- **`get_state_snapshot`** — assembles the full payload sent on every WebSocket broadcast: tournament metadata, ranked player list, current-round matches with player names joined in, 30 most-recent events, and the round start timestamp (normalised to ISO 8601 for JavaScript `Date.parse`).

### `app/services/pairing.py`

The pairing engine. Dispatched via `generate_pairings(mode, players, past_matches)`.

**Four modes:**

- **`swiss`** (`pair_swiss`) — Groups players by score, pairs top half of each group against bottom half (the "fold"), floats unpaired players down to the next group. Falls back to `_max_matching` (recursive branch-and-bound, capped at 14 players) or `_greedy_matching` (fewest-legal-options-first) when the fold leaves players unpaired. A global retry over all players is attempted as a last resort. Bye recipient is chosen by fewest prior byes → lowest score → lowest ELO.
- **`random`** (`pair_random`) — Shuffles the player list and pairs sequentially. Uses the same bye-selection rule as Swiss.
- **`round_robin`** (`pair_round_robin`) — Pre-computes the full schedule with the circle method (fix one seat, rotate the rest; produces n−1 rounds for even n, n rounds for odd n with byes). Each call returns the slice matching the current round number, inferred from how many distinct `round_id`s appear in `past_matches`.
- **`manual`** — Returns `[]`; the host creates pairings one at a time via the API.

**Color assignment** (`_assign_colors`):

1. If one player is on a same-color streak of 2+, give them the opposite color.
2. Player with fewer whites overall gets white.
3. Tie → player with more blacks gets white.
4. Tie → deterministic by player ID.

---

## Templates

All templates extend `base.html`, which provides the `<html>` shell, navigation bar, CSS links, and the `common.js` script tag.

| Template | Role |
|---|---|
| `base.html` | Shell: nav, head, global CSS/JS |
| `index.html` | Landing page with "New Tournament" CTA |
| `new.html` | Form to choose tournament name, pairing mode, and location mode |
| `host.html` | Full host dashboard: player list, pairing controls, match board, dispute resolution, QR code display. Connects to the WebSocket and re-renders on every broadcast. |
| `join.html` | Name-entry form; POSTs to `/api/tournaments/{tid}/players`, then redirects to `/player/{tid}/{pid}` |
| `player.html` | Shows the player's current match and a report-result UI. Connects to the WebSocket and polls `/api/players/{pid}/pending` so the confirm/dispute prompt appears automatically. |
| `projector.html` | Full-screen leaderboard with animated rank changes, live ticker of recent events, and a round timer anchored to `round_started_at`. Connects to the WebSocket. |
| `locked.html` | 403 page shown when someone hits `/host/{tid}` without the token. |

---

## Static assets

### `static/js/common.js`

Shared utilities used by all templates via the global `EP` object:

- `EP.api(method, path, body)` — thin `fetch` wrapper that always sends/receives JSON and throws on non-2xx.
- `EP.connectWs(tid, onMessage)` — opens a WebSocket at `/ws/{tid}`, calls `onMessage` on each parsed message, and auto-reconnects after 1.5 s on close.
- `EP.toast(message, kind)` — injects a self-dismissing toast notification into the page.
- `EP.loaderHtml(label)` — returns an animated chess-board loader HTML string used while data is fetching.

### `static/css/main.css`

Global dark-theme stylesheet. Defines the colour palette (dark backgrounds, green accents), typography, component styles (toasts, buttons, tables, the animated loader), and responsive breakpoints.

### `static/css/projector.css`

Projector-specific overrides: larger type, full-screen layout, the animated ticker strip, rank-change highlight animations, and round-timer display.

---

## Tests

All tests import directly from `app.services.pairing` or patch `app.database.DB_PATH` to point at a temporary file.

| File | What it covers |
|---|---|
| `test_swiss.py` | Simulates multi-round Swiss tournaments (up to 16 players, 12 rounds) and asserts rematch counts, bye distribution, and completeness. |
| `test_tiebreaks.py` | Unit-tests `_recompute_tiebreaks` against hand-computed Buchholz and Sonneborn-Berger values. |
| `test_color_streaks.py` | Tests `_color_streak` and `_assign_colors` to verify the three-in-a-row avoidance rule. |
| `test_quality.py` | Runs pairing quality metrics (rematch rate, bye spread, color balance) across many random simulations. |
| `check_optimality.py` | Verifies that `_max_matching` returns matchings that are actually maximum (no extra pair could be added). |

---

## Data flow

### Player joins

```
Player scans QR → GET /join/{tid} → name form
→ POST /api/tournaments/{tid}/players
→ tournament.add_player() writes to DB
→ api._broadcast_state() calls manager.broadcast()
→ All WebSocket subscribers receive updated state
```

### Result lifecycle

```
Player A reports: POST /api/.../matches/{mid}/report
→ tournament.report_result() sets status='reported'
→ broadcast → Player B's page shows confirm prompt

Player B confirms: POST /api/.../matches/{mid}/confirm
→ tournament.confirm_result() calls _finalize_match()
  → scores updated, tiebreaks recomputed, events logged
→ broadcast → projector re-renders leaderboard with rank animations

(If Player B disputes):
→ status='disputed', dispute event logged
→ broadcast → host dashboard shows resolve button
→ Host POSTs /resolve → _finalize_match() with 'host' as confirmer
```

### Round advancement

```
Host clicks "Start Round" → POST /api/tournaments/{tid}/rounds
→ tournament.start_next_round()
  → guards all previous matches are confirmed
  → calls pairing.generate_pairings(mode, players, past_matches)
  → inserts round + match rows, awards bye points
→ broadcast → host, players, and projector all update simultaneously
```

---

## Dependency summary

```
main.py
 ├── database.py          (init_db)
 ├── routers/pages.py     (page serving)
 ├── routers/api.py       (REST API)
 │    ├── services/tournament.py
 │    │    └── services/pairing.py
 │    └── websocket_manager.py
 └── websocket_manager.py (WebSocket endpoint)
```

`pairing.py` is a pure-Python module with no database access — it receives plain dicts and returns plain dicts, making it straightforwardly testable in isolation.

`database.py` has no knowledge of business rules; it only owns the connection lifecycle and schema.

`tournament.py` is the only module that calls both `database.py` and `pairing.py`.

`websocket_manager.py` is stateless with respect to the database; it only tracks live socket connections in memory.
