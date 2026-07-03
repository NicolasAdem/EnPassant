"""
Auth endpoints + shared "who is logged in" helpers.

Cookie: `ep_session`, httponly, SameSite=Lax. `secure` is left off so the
cookie works over plain http in local dev; put this behind TLS in production
and flip SESSION_COOKIE_SECURE to True.
"""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from typing import Optional

from ..services import auth as auth_svc

router = APIRouter(prefix="/api/auth")

SESSION_COOKIE = "ep_session"
SESSION_COOKIE_SECURE = False  # dev over http; set True behind TLS


# ---------- shared helpers (imported by pages.py and api.py) ----------

def current_user(request: Request) -> Optional[dict]:
    """Resolve the request's session cookie to a user dict, or None."""
    token = request.cookies.get(SESSION_COOKIE)
    return auth_svc.user_for_session(token)


def require_user_api(request: Request) -> dict:
    """FastAPI dependency for JSON endpoints: 401 if not signed in."""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in to continue.")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=auth_svc.SESSION_TTL_DAYS * 24 * 3600,
        httponly=True, samesite="lax", secure=SESSION_COOKIE_SECURE, path="/",
    )


# ---------- models ----------

class SignupReq(BaseModel):
    email: str
    password: str
    display_name: str


class LoginReq(BaseModel):
    email: str
    password: str


# ---------- endpoints ----------

@router.post("/signup")
async def signup(req: SignupReq, response: Response):
    result = auth_svc.create_user(req.email, req.password, req.display_name)
    if "error" in result:
        raise HTTPException(400, result["error"])
    user = result["user"]
    token = auth_svc.create_session(user["id"])
    _set_session_cookie(response, token)
    return user


@router.post("/login")
async def login(req: LoginReq, response: Response):
    user = auth_svc.authenticate(req.email, req.password)
    if not user:
        raise HTTPException(400, "Wrong email or password.")
    token = auth_svc.create_session(user["id"])
    _set_session_cookie(response, token)
    return user


@router.post("/logout")
async def logout(request: Request, response: Response):
    auth_svc.delete_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Not signed in.")
    return user
