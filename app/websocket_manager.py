"""
Manages WebSocket connections grouped by tournament_id.
Anyone watching the projector view, the host dashboard, or a player's screen
subscribes here; broadcasts go out whenever the state changes.
"""

from typing import Dict, Set
from fastapi import WebSocket
import json
import asyncio


class ConnectionManager:
    def __init__(self):
        # tournament_id -> set of WebSocket connections
        self.active: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, tournament_id: str, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            if tournament_id not in self.active:
                self.active[tournament_id] = set()
            self.active[tournament_id].add(websocket)

    async def disconnect(self, tournament_id: str, websocket: WebSocket):
        async with self._lock:
            if tournament_id in self.active:
                self.active[tournament_id].discard(websocket)
                if not self.active[tournament_id]:
                    del self.active[tournament_id]

    async def broadcast(self, tournament_id: str, message: dict):
        """Send a message to all subscribers of a tournament."""
        if tournament_id not in self.active:
            return
        payload = json.dumps(message)
        # Snapshot to avoid mutation during iteration
        async with self._lock:
            conns = list(self.active.get(tournament_id, set()))
        dead = []
        for ws in conns:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.active.get(tournament_id, set()).discard(ws)


manager = ConnectionManager()
