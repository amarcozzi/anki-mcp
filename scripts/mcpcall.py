#!/usr/bin/env python3
"""Call one anki-mcp tool from the command line, doing the OAuth dance once per server.

    scripts/mcpcall.py <server-url> <tool> [json-args]

    scripts/mcpcall.py https://anki-mcp-xxxx.run.app list_decks
    scripts/mcpcall.py http://localhost:8080 next_card '{"deck": "History"}'
    SAVE_IMAGES=/tmp scripts/mcpcall.py http://localhost:8080 reveal '{"card_id": 123}'

Reads OWNER_PASSWORD from .env in the repo root. The access token is cached in
~/.cache/anki-mcp/ keyed by server URL (24 h). Image blocks are printed as a
one-line summary, or written to $SAVE_IMAGES/blockN.jpg when that is set.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

CALLBACK = "https://claude.ai/api/mcp/auth_callback"  # any allow-listed redirect works for the CLI
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def post(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, object, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def login(base: str, password: str) -> str:
    """Dynamic client registration + PKCE authorization code flow; returns an access token."""
    _, _, body = post(
        base + "/register",
        json.dumps({"client_name": "mcpcall", "redirect_uris": [CALLBACK]}).encode(),
        {"Content-Type": "application/json"},
    )
    cid = json.loads(body)["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    form = {
        "client_id": cid,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "response_type": "code",
        "password": password,
    }

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    try:
        r = urllib.request.build_opener(NoRedirect).open(
            urllib.request.Request(base + "/authorize", data=urllib.parse.urlencode(form).encode(), method="POST")
        )
        sys.exit(f"authorize did not redirect (status {r.status}); wrong OWNER_PASSWORD?")
    except urllib.error.HTTPError as e:
        location = e.headers["Location"]
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["code"][0]
    _, _, body = post(
        base + "/token",
        urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": cid,
                "redirect_uri": CALLBACK,
                "code_verifier": verifier,
            }
        ).encode(),
        {},
    )
    return json.loads(body)["access_token"]


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    base, tool = sys.argv[1].rstrip("/"), sys.argv[2]
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}

    cache_dir = os.path.expanduser("~/.cache/anki-mcp")
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, "token-" + re.sub(r"[^a-z0-9]", "", base.lower()))
    if not os.path.exists(cache):
        env = {}
        with open(os.path.join(ROOT, ".env")) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1)
                    env[k] = v
        with open(cache, "w") as f:
            f.write(login(base, env["OWNER_PASSWORD"].strip()))
        os.chmod(cache, 0o600)
    with open(cache) as f:
        token = f.read().strip()

    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    status, _, body = post(
        base + "/mcp",
        json.dumps(rpc).encode(),
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    if status == 401:
        os.remove(cache)
        sys.exit("token rejected (expired?); cache cleared, run again")
    text = body.decode()
    events = [line[5:] for line in text.splitlines() if line.startswith("data:")]
    reply = json.loads(events[0] if events else text)
    if "error" in reply:
        sys.exit(f"RPC ERROR {reply['error']}")
    result = reply["result"]
    if result.get("isError"):
        print("TOOL ERROR:")
    for i, block in enumerate(result["content"]):
        if block["type"] == "text":
            print(block["text"])
        else:
            raw = base64.b64decode(block.get("data", ""))
            print(f"[{block['type']} {block.get('mimeType', '')} {len(raw)} bytes]")
            if os.environ.get("SAVE_IMAGES"):
                with open(os.path.join(os.environ["SAVE_IMAGES"], f"block{i}.jpg"), "wb") as f:
                    f.write(raw)


if __name__ == "__main__":
    main()
