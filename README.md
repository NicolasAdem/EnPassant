# EnPassant ♟️

A live chess tournament app for in-person events. The room sees the leaderboard. Players confirm their own results. The host stays in control.

## What makes it different

- **Live projector view** with animated rank changes — designed to be cast to a TV or projector during the event
- **Dual-confirmation scoring** — one player enters the result, the opponent confirms or disputes, host arbitrates
- **Three pairing modes** per round — Swiss (ELO-based), Random, or Manual
- **QR-code join** — players scan a code, type their name, they're in. No accounts.
- **Live ticker** of recent results, like a sports broadcast
- **Dark, green-accented design** that looks great on a screen

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000

## The flows

- **Host**: go to `/` → "New Tournament" → set name, players, pairing mode → get a QR code and a projector link
- **Player**: scan QR → enter your name → on game end, tap your result on your phone
- **Projector**: open the projector link on the big screen, leave it
- **Confirmation**: opponent gets a live confirm/dispute prompt; if disputed, host resolves

## Stack

- FastAPI (Python) + WebSockets for live updates
- SQLite for persistence (zero config)
- Plain HTML / CSS / JS frontend — no build step
- QR code generation server-side
