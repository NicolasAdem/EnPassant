"""Page-serving routes (HTML)."""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from urllib.parse import quote

from ..services import tournament as svc
from .auth import current_user

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))

router = APIRouter()


def _safe_next(nxt: str) -> str:
    """Only allow same-site absolute paths as a post-login redirect target.
    Anything external (or protocol-relative //evil.com) collapses to '/', which
    closes the open-redirect hole and keeps the value safe to echo into a page."""
    if not nxt or not nxt.startswith("/") or nxt.startswith("//"):
        return "/"
    return nxt


def _login_redirect(request: Request) -> RedirectResponse:
    """Send an unauthenticated visitor to /login, remembering where they were
    headed so we can bounce them back after sign-in (e.g. a scanned join link)."""
    nxt = quote(request.url.path)
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Lobby home: the signed-in user's tournaments. Signed-out visitors land
    on /login."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    lobbies = svc.lobbies_for_user(user["id"])
    return templates.TemplateResponse(request, "home.html", {
        "user": user, "hosting": lobbies["hosting"], "playing": lobbies["playing"],
    })


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    nxt = _safe_next(next)
    if current_user(request):
        return RedirectResponse(nxt, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": nxt, "mode": "login"})


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, next: str = "/"):
    nxt = _safe_next(next)
    if current_user(request):
        return RedirectResponse(nxt, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": nxt, "mode": "signup"})


@router.get("/new", response_class=HTMLResponse)
async def new_tournament_page(request: Request):
    user = current_user(request)
    if not user:
        return _login_redirect(request)
    return templates.TemplateResponse(request, "new.html", {"user": user})


@router.get("/host/{tid}", response_class=HTMLResponse)
async def host_page(request: Request, tid: str, token: str = ""):
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    user = current_user(request)
    # Owner convenience: a signed-in owner reaching their own dashboard without
    # the token in the URL (e.g. from the lobby) is handed the real token so
    # the page's API calls keep working.
    if token != t["host_token"] and user and t.get("host_user_id") == user["id"]:
        token = t["host_token"]
    if not token or token != t["host_token"]:
        return templates.TemplateResponse(request, "locked.html", {"tid": tid}, status_code=403)
    return templates.TemplateResponse(request, "host.html", {
        "tid": tid, "token": token, "tournament_name": t["name"], "user": user,
    })


@router.get("/join/{tid}", response_class=HTMLResponse)
async def join_page(request: Request, tid: str):
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    user = current_user(request)
    if not user:
        return _login_redirect(request)
    # Already joined from this account → straight to their player view.
    existing = svc.get_player_for_user(tid, user["id"])
    if existing:
        return RedirectResponse(f"/player/{tid}/{existing['id']}", status_code=303)
    return templates.TemplateResponse(request, "join.html", {
        "tid": tid, "tournament_name": t["name"], "user": user,
        "started": t["status"] != "lobby",
        "teams": svc.list_teams(tid),
    })


@router.get("/player/{tid}/{pid}", response_class=HTMLResponse)
async def player_page(request: Request, tid: str, pid: str):
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    user = current_user(request)
    if not user:
        return _login_redirect(request)
    p = svc.get_player(pid)
    if not p or p["tournament_id"] != tid:
        raise HTTPException(404, "Player not found in this tournament.")
    # A player linked to an account may only be viewed by that account.
    if p.get("user_id") and p["user_id"] != user["id"]:
        raise HTTPException(403, "That's not your player.")
    return templates.TemplateResponse(request, "player.html", {
        "tid": tid, "pid": pid, "user": user,
        "tournament_name": t["name"], "player_name": p["name"],
    })


@router.get("/projector/{tid}", response_class=HTMLResponse)
async def projector_page(request: Request, tid: str):
    """Public, read-only spectator view — no login required (shareable link)."""
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    return templates.TemplateResponse(request, "projector.html", {
        "tid": tid, "tournament_name": t["name"],
    })


@router.get("/leaderboard/{tid}", response_class=HTMLResponse)
async def leaderboard_page(request: Request, tid: str):
    """Public live leaderboard — same view as the projector, but a friendlier
    URL that players can open from their lobby without being the host."""
    t = svc.get_tournament(tid)
    if not t:
        raise HTTPException(404, "Tournament not found.")
    return templates.TemplateResponse(request, "projector.html", {
        "tid": tid, "tournament_name": t["name"],
    })
