"""
EnPassant main app.
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .database import init_db
from .routers import api, pages
from .services import tournament as svc
from .websocket_manager import manager


app = FastAPI(title="EnPassant", description="Live chess tournament app")

# Initialize DB at import time so it works under uvicorn reload too
init_db()

# Static files (CSS, JS, images)
STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Mount routers
app.include_router(pages.router)
app.include_router(api.router)


@app.websocket("/ws/{tid}")
async def websocket_endpoint(websocket: WebSocket, tid: str):
    """Live updates feed for a tournament. Everyone watching it subscribes here."""
    t = svc.get_tournament(tid)
    if not t:
        await websocket.close(code=1008)
        return
    await manager.connect(tid, websocket)
    try:
        # Send initial snapshot
        snapshot = svc.get_state_snapshot(tid)
        snapshot["tournament"].pop("host_token", None)
        await websocket.send_json({"type": "state", "data": snapshot, "event": None})
        # Keep alive; we don't expect inbound messages, but consume if any
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(tid, websocket)
    except Exception:
        await manager.disconnect(tid, websocket)
