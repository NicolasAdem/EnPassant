"""
HTTP API endpoints. Frontend talks to these via fetch().
WebSocket lives in main.py.
"""

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
import io
import qrcode

from ..services import tournament as svc
from ..websocket_manager import manager


router = APIRouter(prefix="/api")


# ---------- Models ----------

class CreateTournamentReq(BaseModel):
    name: str
    pairing_mode: str = "swiss"  # swiss | round_robin | random | manual
    location_mode: str = "offsite"  # offsite | onsite


class AddPlayerReq(BaseModel):
    name: str
    elo: int = 1200


class StartRoundReq(BaseModel):
    mode_override: Optional[str] = None  # let host choose mode for THIS round


class ManualMatchReq(BaseModel):
    white_player_id: str
    black_player_id: Optional[str] = None  # None = bye


class ReportResultReq(BaseModel):
    player_id: str
    result: str  # 'white' | 'black' | 'draw'


class ConfirmResultReq(BaseModel):
    player_id: str
    agree: bool


class HostResolveReq(BaseModel):
    result: str


class SetTableReq(BaseModel):
    table_number: Optional[int] = None  # null clears the table number


# ---------- Helpers ----------

async def _broadcast_state(tid: str, event: Optional[dict] = None):
    """Push a full state snapshot + optional event hint to all subscribers.

    get_state_snapshot strips host_token, so projector/player subscribers
    never see it (see svc.get_state_snapshot).
    """
    snapshot = svc.get_state_snapshot(tid)
    await manager.broadcast(tid, {"type": "state", "data": snapshot, "event": event})


def _require_host(tid: str, host_token: Optional[str]):
    if not host_token or not svc.verify_host(tid, host_token):
        raise HTTPException(status_code=403, detail="Host token required.")


# ---------- Endpoints ----------

@router.post("/tournaments")
async def create_tournament(req: CreateTournamentReq):
    if not req.name.strip():
        raise HTTPException(400, "Tournament name required.")
    if req.pairing_mode not in ("swiss", "round_robin", "random", "manual"):
        raise HTTPException(400, "Invalid pairing mode.")
    if req.location_mode not in ("offsite", "onsite"):
        raise HTTPException(400, "Invalid location mode.")
    t = svc.create_tournament(req.name.strip()[:60], req.pairing_mode, req.location_mode)
    return t


@router.get("/tournaments/{tid}")
async def get_tournament(tid: str):
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    # Don't leak host_token
    t.pop("host_token", None)
    return t


@router.get("/tournaments/{tid}/state")
async def get_state(tid: str):
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    # get_state_snapshot scrubs host_token at the source.
    return svc.get_state_snapshot(tid)


@router.post("/tournaments/{tid}/players")
async def add_player(tid: str, req: AddPlayerReq):
    if not req.name.strip():
        raise HTTPException(400, "Player name required.")
    p = svc.add_player(tid, req.name, req.elo)
    if not p:
        raise HTTPException(400, "Could not add player. Tournament may be finished or not found.")
    await _broadcast_state(tid, {"kind": "join", "message": f"{p['name']} joined.", "player_id": p["id"]})
    return p


@router.delete("/tournaments/{tid}/players/{pid}")
async def remove_player(tid: str, pid: str, host_token: Optional[str] = None):
    _require_host(tid, host_token)
    ok = svc.remove_player(tid, pid)
    if not ok:
        raise HTTPException(400, "Can only remove players before the tournament starts.")
    await _broadcast_state(tid)
    return {"ok": True}


@router.post("/tournaments/{tid}/rounds")
async def start_round(tid: str, req: StartRoundReq, host_token: Optional[str] = None):
    _require_host(tid, host_token)
    if req.mode_override and req.mode_override not in ("swiss", "round_robin", "random", "manual"):
        raise HTTPException(400, "Invalid mode.")
    result = svc.start_next_round(tid, req.mode_override)
    if result is None:
        raise HTTPException(400, "Need at least 2 players to start a round.")
    if "error" in result:
        raise HTTPException(400, result["error"])
    await _broadcast_state(tid, {"kind": "round_start",
                                  "message": f"Round {result['round_number']} started ({result['mode']}).",
                                  "round": result["round_number"]})
    return result


@router.post("/tournaments/{tid}/matches/manual")
async def add_manual(tid: str, req: ManualMatchReq, host_token: Optional[str] = None):
    _require_host(tid, host_token)
    m = svc.add_manual_match(tid, req.white_player_id, req.black_player_id)
    if not m:
        raise HTTPException(400, "Could not add match.")
    await _broadcast_state(tid)
    return m


@router.post("/tournaments/{tid}/matches/{mid}/report")
async def report(tid: str, mid: str, req: ReportResultReq):
    res = svc.report_result(mid, req.player_id, req.result)
    if res is None:
        raise HTTPException(404, "Match not found or invalid result.")
    if "error" in res:
        raise HTTPException(400, res["error"])
    await _broadcast_state(tid, {"kind": "reported", "message": "Result reported, awaiting confirmation.",
                                  "match_id": mid})
    return res


@router.post("/tournaments/{tid}/matches/{mid}/confirm")
async def confirm(tid: str, mid: str, req: ConfirmResultReq):
    res = svc.confirm_result(mid, req.player_id, req.agree)
    if res is None:
        raise HTTPException(404, "Match not found.")
    if "error" in res:
        raise HTTPException(400, res["error"])
    if res["status"] == "confirmed":
        await _broadcast_state(tid, {"kind": "confirmed", "message": "Result confirmed.", "match_id": mid})
    else:
        await _broadcast_state(tid, {"kind": "disputed", "message": "Result disputed.", "match_id": mid})
    return res


@router.post("/tournaments/{tid}/matches/{mid}/resolve")
async def resolve(tid: str, mid: str, req: HostResolveReq, host_token: Optional[str] = None):
    _require_host(tid, host_token)
    res = svc.host_resolve_match(tid, mid, req.result)
    if res is None:
        raise HTTPException(404, "Match not found.")
    if "error" in res:
        raise HTTPException(400, res["error"])
    await _broadcast_state(tid, {"kind": "confirmed", "message": "Host resolved the match.", "match_id": mid})
    return res


@router.post("/tournaments/{tid}/matches/{mid}/table")
async def set_match_table(tid: str, mid: str, req: SetTableReq, host_token: Optional[str] = None):
    """Host edits the table number for a single match. Only meaningful for
    onsite tournaments; offsite tournaments simply hide the UI for this."""
    _require_host(tid, host_token)
    if req.table_number is not None and (req.table_number < 1 or req.table_number > 9999):
        raise HTTPException(400, "Table number must be between 1 and 9999.")
    res = svc.set_match_table(tid, mid, req.table_number)
    if res is None:
        raise HTTPException(404, "Match not found.")
    if "error" in res:
        raise HTTPException(400, res["error"])
    await _broadcast_state(tid)
    return res


@router.post("/tournaments/{tid}/end")
async def end_tournament(tid: str, host_token: Optional[str] = None):
    _require_host(tid, host_token)
    svc.end_tournament(tid)
    await _broadcast_state(tid, {"kind": "tournament_end", "message": "Tournament finished."})
    return {"ok": True}


@router.get("/players/{pid}/pending")
async def player_pending(pid: str):
    return svc.pending_confirmations_for_player(pid)


@router.get("/tournaments/{tid}/players/{pid}/matches")
async def player_matches(tid: str, pid: str):
    return svc.matches_for_player(pid, tid)


@router.get("/tournaments/{tid}/qrcode.png")
async def qr(tid: str, request: Request):
    """QR code that points to the join page."""
    base = str(request.base_url).rstrip("/")
    url = f"{base}/join/{tid}"
    qr_obj = qrcode.QRCode(box_size=10, border=2)
    qr_obj.add_data(url)
    qr_obj.make(fit=True)
    img = qr_obj.make_image()
    buf = io.BytesIO()
    # PilImage.save takes the format as a positional second arg
    # (named `kind` in older versions, `format` in newer).
    img.save(buf, "PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")