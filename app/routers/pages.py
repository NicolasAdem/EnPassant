"""Page-serving routes (HTML)."""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..services import tournament as svc

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@router.get("/host/{tid}", response_class=HTMLResponse)
async def host_page(request: Request, tid: str, token: str = ""):
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    if not token or token != t["host_token"]:
        # Without the host token, show a friendly error
        return templates.TemplateResponse(request, "locked.html", {"tid": tid}, status_code=403)
    return templates.TemplateResponse(request, "host.html", {
        "tid": tid, "token": token, "tournament_name": t["name"],
    })


@router.get("/join/{tid}", response_class=HTMLResponse)
async def join_page(request: Request, tid: str):
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    return templates.TemplateResponse(request, "join.html", {
        "tid": tid, "tournament_name": t["name"],
    })


@router.get("/player/{tid}/{pid}", response_class=HTMLResponse)
async def player_page(request: Request, tid: str, pid: str):
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    p = svc.get_player(pid)
    if not p or p["tournament_id"] != tid:
        raise HTTPException(404, "Player not found in this tournament.")
    return templates.TemplateResponse(request, "player.html", {
        "tid": tid, "pid": pid,
        "tournament_name": t["name"], "player_name": p["name"],
    })


@router.get("/projector/{tid}", response_class=HTMLResponse)
async def projector_page(request: Request, tid: str):
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    return templates.TemplateResponse(request, "projector.html", {
        "tid": tid, "tournament_name": t["name"],
    })
