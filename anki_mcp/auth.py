"""Minimal single-user OAuth 2.1 authorization server for claude.ai connectors.

Stateless: auth codes and tokens are HS256 JWTs signed with one key, the consent
step is a single owner password, and redirect URIs are restricted to an
allowlist so a stolen code cannot be sent anywhere else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastmcp.server.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

log = logging.getLogger(__name__)

CODE_TTL = 5 * 60
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 180 * 24 * 3600

DEFAULT_REDIRECTS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Tokens:
    """HS256 JWT mint/verify with no external dependency."""

    def __init__(self, key: bytes):
        self.key = key

    def sign(self, claims: dict) -> str:
        head = _b64(b'{"alg":"HS256","typ":"JWT"}')
        body = _b64(json.dumps(claims, separators=(",", ":")).encode())
        sig = hmac.new(self.key, f"{head}.{body}".encode(), hashlib.sha256).digest()
        return f"{head}.{body}.{_b64(sig)}"

    def verify(self, token: str, want_type: str, now: float | None = None) -> dict | None:
        try:
            head, body, sig = token.split(".")
            expected = hmac.new(self.key, f"{head}.{body}".encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(_unb64(sig), expected):
                return None
            claims = json.loads(_unb64(body))
        except (ValueError, TypeError, UnicodeDecodeError):  # malformed token
            return None
        if claims.get("typ") != want_type:
            return None
        if (now or time.time()) >= claims.get("exp", 0):
            return None
        return claims


class _FailLimiter:
    """Slow down password guessing. Per process, which is fine for one instance."""

    def __init__(self, limit: int = 5, window: float = 60.0):
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> list[float]:
        keep = [t for t in self.fails.get(ip, []) if now - t < self.window]
        self.fails[ip] = keep
        return keep

    def blocked(self, ip: str, now: float) -> bool:
        with self.lock:
            return len(self._prune(ip, now)) >= self.limit

    def fail(self, ip: str, now: float) -> None:
        with self.lock:
            self.fails[ip] = self._prune(ip, now) + [now]


@dataclass
class OAuthServer:
    base_url: str
    owner_password: str
    signing_key: bytes
    extra_redirects: list[str] = field(default_factory=list)
    now: callable = time.time

    def __post_init__(self):
        self.tokens = Tokens(self.signing_key)
        self.redirects = set(DEFAULT_REDIRECTS) | set(self.extra_redirects)
        self.limiter = _FailLimiter()

    # --- FastMCP integration ----------------------------------------------------------

    def auth_provider(self) -> RemoteAuthProvider:
        """Bearer verification plus the protected-resource metadata FastMCP serves."""
        return RemoteAuthProvider(
            token_verifier=_JWTVerifier(self),
            authorization_servers=[self.base_url],
            base_url=self.base_url,
            resource_name="anki-mcp",
        )

    def register_routes(self, mcp) -> None:
        """Mount the authorization-server endpoints on the FastMCP app."""
        mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])(self.metadata)
        mcp.custom_route("/register", methods=["POST"])(self.register)
        mcp.custom_route("/authorize", methods=["GET"])(self.authorize_form)
        mcp.custom_route("/authorize", methods=["POST"])(self.authorize_submit)
        mcp.custom_route("/token", methods=["POST"])(self.token)

    # --- endpoints ------------------------------------------------------------------------

    async def metadata(self, _: Request) -> Response:
        return JSONResponse(
            {
                "issuer": self.base_url,
                "authorization_endpoint": f"{self.base_url}/authorize",
                "token_endpoint": f"{self.base_url}/token",
                "registration_endpoint": f"{self.base_url}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
            }
        )

    async def register(self, request: Request) -> Response:
        """RFC 7591 dynamic client registration. Nothing is stored; the redirect
        allowlist is what actually protects the flow."""
        try:
            body = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            return _oauth_error(400, "invalid_client_metadata", "bad JSON")
        uris = body.get("redirect_uris") or []
        for u in uris:
            if u not in self.redirects:
                return _oauth_error(400, "invalid_redirect_uri", f"redirect URI not allowed: {u}")
        return JSONResponse(
            {
                "client_id": "anki-mcp-" + secrets.token_hex(8),
                "client_id_issued_at": int(self.now()),
                "redirect_uris": uris,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": body.get("client_name", ""),
            },
            status_code=201,
        )

    _FIELDS = (
        "client_id",
        "redirect_uri",
        "state",
        "code_challenge",
        "code_challenge_method",
        "response_type",
        "resource",
    )

    def _validate(self, p: dict) -> str | None:
        if not p.get("client_id"):
            return "missing client_id"
        if p.get("redirect_uri") not in self.redirects:
            return "redirect_uri not allowed"
        if p.get("response_type") != "code":
            return "response_type must be code"
        if p.get("code_challenge_method") != "S256" or not p.get("code_challenge"):
            return "PKCE with S256 is required"
        return None

    def _form(self, p: dict, error: str = "", status: int = 200) -> Response:
        hidden = "".join(
            f'<input type="hidden" name="{k}" value="{html.escape(p.get(k, ""))}">' for k in self._FIELDS
        )
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        page = f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>anki-mcp</title>
<style>body{{font-family:system-ui;max-width:24rem;margin:4rem auto;padding:0 1rem}}
input,button{{font-size:1rem;padding:.5rem;width:100%;box-sizing:border-box;margin:.25rem 0}}.err{{color:#b00}}</style>
<h1>anki-mcp</h1><p><b>{html.escape(p.get("client_id", ""))}</b> wants to review your Anki cards.</p>{err}
<form method="post" action="/authorize">{hidden}
<input type="password" name="password" placeholder="Owner password" autofocus autocomplete="current-password">
<button type="submit">Allow</button></form>"""
        return HTMLResponse(page, status_code=status)

    async def authorize_form(self, request: Request) -> Response:
        p = {k: request.query_params.get(k, "") for k in self._FIELDS}
        if msg := self._validate(p):
            return Response(msg, status_code=400)
        return self._form(p)

    async def authorize_submit(self, request: Request) -> Response:
        form = await request.form()
        p = {k: str(form.get(k, "")) for k in self._FIELDS}
        if msg := self._validate(p):
            return Response(msg, status_code=400)
        ip = _client_ip(request)
        now = self.now()
        if self.limiter.blocked(ip, now):
            return self._form(p, "Too many attempts. Try again in a minute.", 429)
        if not hmac.compare_digest(str(form.get("password", "")).encode(), self.owner_password.encode()):
            self.limiter.fail(ip, now)
            log.warning("authorize: wrong owner password from %s", ip)
            return self._form(p, "Wrong password.", 401)
        code = self.tokens.sign(
            {
                "typ": "code",
                "cid": p["client_id"],
                "ru": p["redirect_uri"],
                "cc": p["code_challenge"],
                "jti": secrets.token_hex(16),
                "iat": int(now),
                "exp": int(now) + CODE_TTL,
            }
        )
        url = urlparse(p["redirect_uri"])
        q = dict(parse_qsl(url.query))
        q["code"] = code
        if p["state"]:
            q["state"] = p["state"]
        dest = urlunparse(url._replace(query=urlencode(q)))
        log.info("authorize: issued code to %s", p["client_id"])
        return RedirectResponse(dest, status_code=302)

    async def token(self, request: Request) -> Response:
        form = await request.form()
        grant = form.get("grant_type")
        client_id = str(form.get("client_id", ""))
        now = self.now()
        if grant == "authorization_code":
            c = self.tokens.verify(str(form.get("code", "")), "code", now)
            if not c:
                return _oauth_error(400, "invalid_grant", "code invalid or expired")
            if client_id and client_id != c["cid"]:
                return _oauth_error(400, "invalid_grant", "client_id mismatch")
            ru = str(form.get("redirect_uri", ""))
            if ru and ru != c["ru"]:
                return _oauth_error(400, "invalid_grant", "redirect_uri mismatch")
            digest = hashlib.sha256(str(form.get("code_verifier", "")).encode()).digest()
            if _b64(digest) != c["cc"]:
                return _oauth_error(400, "invalid_grant", "PKCE verification failed")
            return self._issue(c["cid"], now)
        if grant == "refresh_token":
            c = self.tokens.verify(str(form.get("refresh_token", "")), "refresh", now)
            if not c:
                return _oauth_error(400, "invalid_grant", "refresh token invalid or expired")
            if client_id and client_id != c["cid"]:
                return _oauth_error(400, "invalid_grant", "client_id mismatch")
            return self._issue(c["cid"], now)
        return _oauth_error(400, "unsupported_grant_type", "")

    def _issue(self, client_id: str, now: float) -> Response:
        def tok(typ: str, ttl: int) -> str:
            return self.tokens.sign(
                {
                    "typ": typ,
                    "cid": client_id,
                    "jti": secrets.token_hex(16),
                    "iat": int(now),
                    "exp": int(now) + ttl,
                }
            )

        return JSONResponse(
            {
                "access_token": tok("access", ACCESS_TTL),
                "token_type": "Bearer",
                "expires_in": ACCESS_TTL,
                "refresh_token": tok("refresh", REFRESH_TTL),
            },
            headers={"Cache-Control": "no-store"},
        )


class _JWTVerifier(TokenVerifier):
    def __init__(self, server: OAuthServer):
        super().__init__(base_url=server.base_url)
        self.server = server

    async def verify_token(self, token: str) -> AccessToken | None:
        c = self.server.tokens.verify(token, "access", self.server.now())
        if not c:
            return None
        return AccessToken(token=token, client_id=c["cid"], scopes=[], expires_at=c["exp"], subject="owner")


def _oauth_error(status: int, code: str, desc: str) -> Response:
    return JSONResponse({"error": code, "error_description": desc}, status_code=status)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"
