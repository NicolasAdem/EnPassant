# EnPassant ♟️

Casual chess tournaments for you and your friends. Create a lobby, share a code, and let the room watch a live leaderboard while players confirm their own results.

## What makes it different

- **Accounts + lobbies** — sign in and see every tournament you're hosting or playing in, and which of your matches are ready right now
- **Live projector view** with animated rank changes — cast it to a TV so the whole room follows along
- **Dual-confirmation scoring** — one player reports the result, the opponent confirms or disputes, the host arbitrates
- **Teams mode** — split into 2–4 sides; teammates never face each other and each team's score is the sum of its members'
- **Locked pairing mode** — pick Swiss, Round-robin, Random or Manual at creation; it stays fixed so standings stay coherent (with a recommendation based on your headcount)
- **Automatic rounds** — optionally let the next round start on its own once every game is confirmed
- **A settings drawer** — rename the tournament, toggle auto-rounds, switch themes (chessboard green by default, plus minimalist light/dark), upload or link a background image for your group, and share links
- **Play as host** — tick "I'm playing too" and the host joins their own event
- **Shareable Leaderboard link** — players (and spectators) open the live leaderboard from their lobby; anyone can watch, no login needed

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000 and create an account.

## The flows

- **Host**: sign in → "Create tournament" → set name, pairing mode, and (optionally) teams → share the code/QR, open the projector on the big screen, run each round from the dashboard
- **Player**: sign in → enter the code (or scan the QR) → pick a team if it's a team event → after each game, tap your result; your opponent confirms it
- **Spectator**: open the projector link — it's public and read-only
- **Disputes**: if an opponent disputes a result, the host resolves it from the dashboard

## Stack

- FastAPI (Python) + WebSockets for live updates
- SQLite for persistence (zero config)
- Email + password accounts, hashed with PBKDF2 (standard library — no extra dependencies)
- Plain HTML / CSS / JS frontend — no build step
- QR code generation server-side

## Tests

```bash
python -m pytest -q
```

Covers the pairing engine (Swiss correctness, tiebreaks, color balance, byes), accounts & sessions, the pairing-mode lock, teams, and the security regressions (host-token scrubbing, cross-tournament guards, XSS).
