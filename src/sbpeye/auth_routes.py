"""Sign-in, sign-out, admin user management, and the guards the app hangs off.

The authentication boundary is middleware with an allowlist, not a `Depends` on each
route. With 74 routes and more arriving, per-route opt-in means the failure mode of
forgetting one is a publicly readable endpoint, discovered by someone else. Inverted, the
failure mode is a route that unexpectedly demands a login, discovered immediately by the
person who added it. Only the second kind is safe to get wrong.

The admin boundary is per-route (`Depends(require_admin)`), because it genuinely varies
route by route and the list is the one in the deployment plan, 7.3.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    AuthConfigError,
    authenticate,
    create_user,
    issue_session,
    normalize_email,
    read_session,
    secret_key,
    seed_first_admin,
)
from .database import AppSessionLocal, get_app_db
from .models import ChatSession, User

router = APIRouter()

# Reachable without a session. Everything not matched here requires one.
#
# `/healthz` is here because a health check has no cookie to present and a probe that
# 401s reads as an unhealthy container — the platform would roll a perfectly good deploy.
# It exposes only whether the databases open, which is not worth protecting.
PUBLIC_PATHS = frozenset({
    "/healthz",
    "/login",
    "/api/auth/login",
    "/api/auth/logout",
})

# Static assets: the login page has to be able to style itself, and these are shipped in
# the image rather than derived from the corpus.
PUBLIC_PREFIXES = ("/static/", "/spa/assets/", "/favicon")


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def verify_auth_configuration() -> None:
    """Fail the boot rather than the first request.

    `secret_key()` is only consulted when a session is issued or read, so a deployment
    missing it would start, pass its health check, serve the login page and fail at the
    moment someone tried to sign in. Checking here turns that into a startup crash with
    the reason in the logs.
    """
    secret_key()


def adopt_orphaned_chat_sessions(app_db: Session, user: User) -> int:
    """Give pre-authentication chat sessions to the bootstrap admin.

    Only ever runs alongside first-admin seeding. On a fresh deployment the application
    database is empty and this is a no-op; on a developer's machine it is the difference
    between keeping their existing chat history and having it silently vanish from the
    UI, since every list query is now filtered by owner.
    """
    orphaned = (
        app_db.query(ChatSession).filter(ChatSession.user_id.is_(None)).all()
    )
    for session in orphaned:
        session.user_id = user.id
    if orphaned:
        app_db.commit()
        logging.warning(
            "Adopted %d pre-authentication chat session(s) for %s",
            len(orphaned), user.email,
        )
    return len(orphaned)


def bootstrap_admin() -> None:
    """Seed the first admin at startup, and hand it anything left ownerless."""
    app_db = AppSessionLocal()
    try:
        user = seed_first_admin(app_db)
        if user is not None:
            adopt_orphaned_chat_sessions(app_db, user)
    finally:
        app_db.close()


# --------------------------------------------------------------------------- guards


def current_user(request: Request) -> User:
    """The signed-in user, as established by the middleware.

    Never re-reads the cookie: the middleware has already resolved it, and a second
    lookup here would be a second place for the two to disagree.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        # Only reachable if a route on the public allowlist asks for a user.
        raise _unauthorized()
    return user


def require_admin(request: Request) -> User:
    user = current_user(request)
    if not user.is_admin:
        raise _forbidden()
    return user


def admin_only(message: str):
    """`require_admin` with a reason specific to the route.

    Worth the indirection for endpoints where a bare 403 reads as a bug rather than a
    policy — `/api/circulars/open` is the case the deployment plan calls out (7.3): a
    tester pasting an SBP link gets a refusal, and it has to say why.
    """

    def dependency(request: Request) -> User:
        user = current_user(request)
        if not user.is_admin:
            raise _forbidden(message)
        return user

    return dependency


def _unauthorized():
    from fastapi import HTTPException

    return HTTPException(status_code=401, detail="Sign in to continue.")


def _forbidden(message: str | None = None):
    from fastapi import HTTPException

    return HTTPException(
        status_code=403,
        detail=message or "This action is limited to administrators on this deployment.",
    )


def resolve_request_user(request: Request) -> User | None:
    """The user a request's cookie vouches for, or None."""
    user_id = read_session(request.cookies.get(SESSION_COOKIE))
    if not user_id:
        return None
    app_db = AppSessionLocal()
    try:
        # Looked up every request rather than trusted from the cookie, so that deleting a
        # user or clearing their admin flag takes effect immediately instead of whenever
        # their cookie happens to expire.
        return app_db.query(User).filter(User.id == user_id).first()
    finally:
        app_db.close()


def _set_session_cookie(response, user: User, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(user),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        # Lax, not Strict: Strict would drop the cookie when a tester follows a link to
        # the app from an email or chat, presenting a login page to someone who is
        # already signed in. Lax still refuses to send it on cross-site POSTs.
        samesite="lax",
        secure=secure,
        path="/",
    )


# --------------------------------------------------------------------------- routes


_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in - SBPEye</title>
<style>
  :root { color-scheme: light dark;
    --bg:#f6f7f9; --card:#fff; --fg:#16181d; --muted:#666c7a; --line:#dfe3ea;
    --accent:#1f5fd0; --err:#b3261e; }
  @media (prefers-color-scheme: dark) { :root {
    --bg:#14161a; --card:#1c1f25; --fg:#e8eaed; --muted:#9aa2b1; --line:#2c313a;
    --accent:#6f9bea; --err:#f2b8b5; } }
  * { box-sizing:border-box }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg);
    color:var(--fg); font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  form { background:var(--card); padding:2rem; border-radius:12px; border:1px solid var(--line);
    width:min(92vw,23rem); box-shadow:0 1px 3px rgba(0,0,0,.06) }
  h1 { margin:0 0 .25rem; font-size:1.25rem }
  p.sub { margin:0 0 1.5rem; color:var(--muted); font-size:.875rem }
  label { display:block; margin-bottom:1rem; font-size:.8125rem; color:var(--muted) }
  input { width:100%; margin-top:.375rem; padding:.625rem .75rem; font-size:.9375rem;
    border:1px solid var(--line); border-radius:7px; background:var(--bg); color:var(--fg) }
  input:focus { outline:2px solid var(--accent); outline-offset:-1px; border-color:transparent }
  button { width:100%; padding:.6875rem; font-size:.9375rem; font-weight:600; cursor:pointer;
    border:0; border-radius:7px; background:var(--accent); color:#fff }
  button[disabled] { opacity:.6; cursor:progress }
  .err { margin:0 0 1rem; padding:.625rem .75rem; border-radius:7px; font-size:.8125rem;
    color:var(--err); border:1px solid var(--err); display:none }
  .err.on { display:block }
</style></head><body>
<form id="f">
  <h1>SBPEye</h1>
  <p class="sub">Sign in to continue.</p>
  <p class="err" id="e" role="alert"></p>
  <label>Email<input name="email" type="email" autocomplete="username" required autofocus></label>
  <label>Password<input name="password" type="password" autocomplete="current-password" required></label>
  <button id="b" type="submit">Sign in</button>
</form>
<script>
const f=document.getElementById('f'),e=document.getElementById('e'),b=document.getElementById('b');
f.addEventListener('submit',async ev=>{
  ev.preventDefault(); e.classList.remove('on'); b.disabled=true; b.textContent='Signing in...';
  try{
    const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:f.email.value,password:f.password.value})});
    if(r.ok){
      // Only same-origin relative paths. A bare `next` would make this an open redirect:
      // /login?next=https://evil.example would bounce a signed-in user straight there.
      const n=new URLSearchParams(location.search).get('next')||'/';
      location.replace(/^\/(?!\/)/.test(n)?n:'/'); return; }
    const d=await r.json().catch(()=>({}));
    e.textContent=d.error||'Sign in failed.'; e.classList.add('on');
  }catch(_){ e.textContent='Could not reach the server.'; e.classList.add('on'); }
  b.disabled=false; b.textContent='Sign in';
});
</script></body></html>"""


@router.get("/login")
async def login_page(request: Request):
    """A plain server-rendered page, deliberately not part of the SPA.

    The bundle is behind the same gate as everything else, so a login view inside it
    would have to be reachable without a session — which means carving a hole in the
    allowlist for the whole SPA. A standalone page keeps the hole the size of one route.
    """
    if resolve_request_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_LOGIN_PAGE)


@router.post("/api/auth/login")
async def login(request: Request, app_db: Session = Depends(get_app_db)):
    payload = await _json_or_form(request)
    email = str(payload.get("email") or "")
    password = str(payload.get("password") or "")

    user = authenticate(app_db, email, password)
    if user is None:
        # One message for both a wrong password and an unknown address. Distinguishing
        # them turns the form into a way to enumerate who has an account.
        return JSONResponse({"error": "Incorrect email or password."}, status_code=401)

    response = JSONResponse({
        "id": user.id,
        "email": user.email,
        "is_admin": bool(user.is_admin),
    })
    _set_session_cookie(response, user, secure=request.url.scheme == "https")
    return response


@router.post("/api/auth/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/api/auth/me")
async def me(user: User = Depends(current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "is_admin": bool(user.is_admin),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


@router.get("/api/settings/ai")
async def get_my_ai_settings(user: User = Depends(current_user)):
    """This user's own provider configuration.

    Not admin-gated, unlike `/api/settings`: that one is deployment configuration
    (embeddings, which must match the shipped index) and stays with the admin. This is
    the credential the user pays with, so it has to be theirs to set.
    """
    from .ai import get_provider_definition, normalize_provider

    provider = (user.ai_provider or "").strip()
    definition = get_provider_definition(normalize_provider(provider)) if provider else None
    return {
        "provider": provider,
        "base_url": user.ai_base_url or (definition.default_base_url if definition else ""),
        "model": user.ai_model or (definition.default_model if definition else ""),
        "chat_model": user.ai_chat_model or "",
        # Never the key, not even to its owner. It is write-only from here: the browser
        # has no use for it, and echoing it back puts a live credential in every response
        # body, browser cache and screenshot of this page.
        "api_key_set": bool(user.ai_api_key_encrypted),
        "configured": bool(provider),
    }


@router.put("/api/settings/ai")
async def set_my_ai_settings(
    request: Request,
    user: User = Depends(current_user),
    app_db: Session = Depends(get_app_db),
):
    from .ai import encrypt_key_for_storage, normalize_provider

    payload = await _json_or_form(request)
    provider = str(payload.get("provider") or "").strip()
    if provider:
        provider = normalize_provider(provider)

    row = app_db.query(User).filter(User.id == user.id).first()
    if row is None:
        return JSONResponse({"error": "No such user."}, status_code=404)

    row.ai_provider = provider or None
    row.ai_base_url = str(payload.get("base_url") or "").strip() or None
    row.ai_model = str(payload.get("model") or "").strip() or None
    row.ai_chat_model = str(payload.get("chat_model") or "").strip() or None

    # An absent `api_key` leaves the stored one alone, so a user can change their model
    # without re-typing the credential. An explicit empty string clears it.
    if "api_key" in payload:
        api_key = str(payload.get("api_key") or "")
        row.ai_api_key_encrypted = encrypt_key_for_storage(api_key) or None

    app_db.commit()
    return {"ok": True, "provider": row.ai_provider or "", "api_key_set": bool(row.ai_api_key_encrypted)}


@router.get("/api/admin/users")
async def list_users(
    _: User = Depends(require_admin), app_db: Session = Depends(get_app_db)
):
    users = app_db.query(User).order_by(User.created_at).all()
    return {
        "items": [
            {
                "id": u.id,
                "email": u.email,
                "is_admin": bool(u.is_admin),
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "last_login_at": (
                    u.last_login_at.isoformat() if u.last_login_at else None
                ),
            }
            for u in users
        ]
    }


@router.post("/api/admin/users")
async def add_user(
    request: Request,
    admin: User = Depends(require_admin),
    app_db: Session = Depends(get_app_db),
):
    """Create a tester. There is no self-registration route on this deployment.

    Chat is per-user and unmetered (deployment plan, 7.5), so every account is a
    standing claim on the provider bill. Keeping creation with the admin keeps that
    list short and known.
    """
    payload = await _json_or_form(request)
    try:
        user = create_user(
            app_db,
            email=str(payload.get("email") or ""),
            password=str(payload.get("password") or ""),
            is_admin=bool(payload.get("is_admin")),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    logging.warning("%s created user %s", admin.email, user.email)
    return {"id": user.id, "email": user.email, "is_admin": bool(user.is_admin)}


@router.delete("/api/admin/users/{user_id}")
async def delete_user(
    user_id: str,
    admin: User = Depends(require_admin),
    app_db: Session = Depends(get_app_db),
):
    user = app_db.query(User).filter(User.id == user_id).first()
    if user is None:
        return JSONResponse({"error": "No such user."}, status_code=404)
    if user.id == admin.id:
        return JSONResponse(
            {"error": "You cannot delete your own account."}, status_code=400
        )
    if user.is_admin:
        remaining = (
            app_db.query(User)
            .filter(User.is_admin == 1, User.id != user.id)
            .count()
        )
        if remaining == 0:
            # Otherwise the deployment has no way back in short of a shell on the volume.
            return JSONResponse(
                {"error": "This is the only administrator; promote another first."},
                status_code=400,
            )

    # Their chat goes with them. It is theirs, nobody else can read it, and leaving it
    # behind would leave rows pointing at a user id that resolves to nothing.
    app_db.query(ChatSession).filter(ChatSession.user_id == user.id).delete(
        synchronize_session=False
    )
    app_db.delete(user)
    app_db.commit()
    logging.warning("%s deleted user %s", admin.email, user.email)
    return {"ok": True}


async def _json_or_form(request: Request) -> dict:
    """Accept either, so the plain HTML login form and the SPA can post the same route."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}
    try:
        return dict(await request.form())
    except Exception:
        return {}


__all__ = [
    "PUBLIC_PATHS",
    "AuthConfigError",
    "bootstrap_admin",
    "current_user",
    "is_public_path",
    "normalize_email",
    "require_admin",
    "resolve_request_user",
    "router",
    "verify_auth_configuration",
    "HTMLResponse",
    "RedirectResponse",
]
