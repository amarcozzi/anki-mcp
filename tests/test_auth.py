import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastmcp import FastMCP

from anki_mcp.auth import OAuthServer

KEY = b"0123456789abcdef0123456789abcdef"
CB = "https://claude.ai/api/mcp/auth_callback"


def make_app():
    oauth = OAuthServer(base_url="http://test", owner_password="hunter2", signing_key=KEY)
    mcp = FastMCP("t", auth=oauth.auth_provider())
    oauth.register_routes(mcp)

    @mcp.tool
    def ping() -> str:
        return "pong"

    return oauth, mcp.http_app(path="/mcp", stateless_http=True)


@pytest.fixture
def app():
    return make_app()[1]


def rpc(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.mark.anyio
async def test_full_flow(app):
    # FastMCP's session manager lives in the app lifespan, which ASGITransport does not run.
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        r = await client.post("/mcp", json=rpc("initialize"), headers=H)
        assert r.status_code == 401
        assert "oauth-protected-resource" in r.headers.get("www-authenticate", "")

        r = await client.get("/.well-known/oauth-authorization-server")
        assert r.json()["registration_endpoint"] == "http://test/register"
        r = await client.get("/.well-known/oauth-protected-resource/mcp")
        assert r.status_code == 200 and r.json()["authorization_servers"] == ["http://test"]

        r = await client.post("/register", json={"client_name": "claude", "redirect_uris": [CB]})
        assert r.status_code == 201
        cid = r.json()["client_id"]
        r = await client.post("/register", json={"redirect_uris": ["https://evil.example/cb"]})
        assert r.status_code == 400

        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        form = {
            "client_id": cid,
            "redirect_uri": CB,
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "response_type": "code",
        }

        r = await client.get("/authorize", params=form)
        assert r.status_code == 200 and "Owner password" in r.text
        r = await client.post("/authorize", data={**form, "password": "nope"})
        assert r.status_code == 401
        r = await client.post("/authorize", data={**form, "password": "hunter2"})
        assert r.status_code == 302
        loc = urlparse(r.headers["location"])
        assert loc.netloc == "claude.ai"
        q = parse_qs(loc.query)
        assert q["state"] == ["xyz"]
        code = q["code"][0]

        r = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": cid,
                "redirect_uri": CB,
                "code_verifier": "wrong",
            },
        )
        assert r.status_code == 400
        r = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": cid,
                "redirect_uri": CB,
                "code_verifier": verifier,
            },
        )
        assert r.status_code == 200, r.text
        tok = r.json()

        auth = {**H, "Authorization": f"Bearer {tok['access_token']}"}
        r = await client.post(
            "/mcp",
            json=rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            ),
            headers=auth,
        )
        assert r.status_code == 200, r.text
        r = await client.post(
            "/mcp", json=rpc("tools/call", {"name": "ping", "arguments": {}}, 2), headers=auth
        )
        assert r.status_code == 200 and "pong" in r.text

        r = await client.post(
            "/mcp", json=rpc("initialize"), headers={**H, "Authorization": f"Bearer {tok['refresh_token']}"}
        )
        assert r.status_code == 401

        r = await client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid},
        )
        assert r.status_code == 200


def test_token_expiry_and_tamper():
    oauth, _ = make_app()
    now = time.time()
    tok = oauth.tokens.sign(
        {"typ": "access", "cid": "c", "jti": "n", "iat": int(now), "exp": int(now) + 3600}
    )
    assert oauth.tokens.verify(tok, "access", now)
    assert oauth.tokens.verify(tok, "access", now + 7200) is None
    assert oauth.tokens.verify(tok + "x", "access", now) is None
    assert oauth.tokens.verify(tok, "refresh", now) is None
