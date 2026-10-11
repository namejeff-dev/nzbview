"""Local-only defaults, optional token login, and same-origin write protection."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from config import placeholder
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

LOGIN_HTML = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · nzbview</title><link rel="stylesheet" href="/static/app.css?v=2.15.2">
<body class="setup-body"><main class="setup-card"><h1>Sign in to nzbview</h1>
<p>Enter the access token configured by the person running this installation.</p>
<form method="post" action="/login"><label for="token">Access token</label>
<input id="token" name="token" type="password" required autocomplete="current-password">
<button class="primary-btn" type="submit">Sign in</button></form></main></body></html>'''


def access_token() -> str:
    return os.environ.get("NZBVIEW_ACCESS_TOKEN", "")


def session_value(token: str) -> str:
    expires = str(int(time.time()) + 12 * 3600)
    signature = hmac.new(token.encode(), expires.encode(), hashlib.sha256).hexdigest()
    return expires + "." + signature


def authorized(request) -> bool:
    token = access_token()
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer ") and secrets.compare_digest(header[7:], token):
        return True
    cookie = request.cookies.get("nzbview_session", "")
    try:
        expires, signature = cookie.split(".", 1)
        expected = hmac.new(token.encode(), expires.encode(), hashlib.sha256).hexdigest()
        return int(expires) > time.time() and secrets.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def write_allowed(request) -> bool:
    origin = request.headers.get("Origin")
    expected = os.environ.get("NZBVIEW_PUBLIC_ORIGIN") or str(request.base_url).rstrip("/")
    if origin and origin.rstrip("/") != expected.rstrip("/"):
        return False
    if request.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
        return False
    # Non-browser clients must intentionally mark a write request.
    return bool(origin or request.headers.get("X-Nzbview-Request") == "1")


def install_security(app):
    token = access_token()
    if token and (len(token) < 24 or placeholder(token)):
        raise ValueError("NZBVIEW_ACCESS_TOKEN must be a generated, non-placeholder token with at least 24 characters.")
    hosts = [h.strip() for h in os.environ.get("NZBVIEW_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if h.strip()]
    if not hosts or "*" in hosts:
        raise ValueError("NZBVIEW_ALLOWED_HOSTS must list explicit hostnames or IP addresses.")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts, www_redirect=False)

    @app.middleware("http")
    async def protect(request, call_next):
        remote = request.client.host if request.client else ""
        if not access_token() and remote not in {"127.0.0.1", "::1", "localhost"}:
            return JSONResponse({"error": "Remote access requires NZBVIEW_ACCESS_TOKEN."}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not write_allowed(request):
            return JSONResponse({"error": "Write request must come from the same origin."}, status_code=403)
        if access_token() and not request.url.path.startswith("/static/") and request.url.path != "/login":
            if not authorized(request):
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"error": "Authentication required."}, status_code=401)
                return RedirectResponse("/login", status_code=303)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/login", include_in_schema=False)
    def login_page():
        return HTMLResponse(LOGIN_HTML) if access_token() else RedirectResponse("/")

    @app.post("/login", include_in_schema=False)
    async def login(request: Request):
        body = await request.body()
        if len(body) > 4096:
            return HTMLResponse("Invalid login request.", status_code=400)
        supplied = parse_qs(body.decode("utf-8", errors="replace")).get("token", [""])[0]
        token = access_token()
        if not token or not secrets.compare_digest(supplied, token):
            return HTMLResponse(LOGIN_HTML.replace("<h1>", "<p>Invalid access token.</p><h1>"), status_code=401)
        response = RedirectResponse("/", status_code=303)
        scheme = urlsplit(os.environ.get("NZBVIEW_PUBLIC_ORIGIN") or str(request.base_url)).scheme
        response.set_cookie("nzbview_session", session_value(token), max_age=12 * 3600,
                            httponly=True, samesite="strict", secure=scheme == "https")
        return response

    @app.post("/logout", include_in_schema=False)
    def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("nzbview_session")
        return response
