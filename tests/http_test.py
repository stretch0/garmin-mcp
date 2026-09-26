"""The HTTP transport and its OAuth sign-in, end to end, with no network.

Drives the same ASGI app the server runs, the way Claude does: discovery,
dynamic client registration, /authorize with PKCE, Google sign-in (stubbed at
the HTTP layer, so the ID-token checks run for real), the consent page, the
code exchange, MCP calls with the token, and refresh-token rotation.

    .venv/bin/python tests/http_test.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PUBLIC = "https://mini.example.ts.net"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
GOOGLE_CLIENT = "google-client-id.apps.googleusercontent.com"
ME = "runner@example.com"

STATE_DIR = Path(tempfile.mkdtemp())
os.environ.update(
    GARMIN_MCP_TRANSPORT="http",
    GARMIN_MCP_HOST="0.0.0.0",
    GARMIN_MCP_PUBLIC_URL=PUBLIC,
    GARMIN_MCP_GOOGLE_CLIENT_ID=GOOGLE_CLIENT,
    GARMIN_MCP_GOOGLE_CLIENT_SECRET="google-secret",
    GARMIN_MCP_ALLOWED_EMAILS=f"Other@example.com, {ME.upper()}",
    GARMIN_MCP_TOKENS=str(STATE_DIR / "tokens.json"),
    GARMIN_EMAIL="test@example.com",
    GARMIN_PASSWORD="hunter2",
)

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from garmin_mcp import remote, session as session_mod  # noqa: E402
from garmin_mcp import server  # noqa: E402
from garmin_mcp.oauth import OAuthConfig  # noqa: E402
from tests.fake_garmin import FakeGarmin  # noqa: E402

session_mod.build_client = lambda **_: FakeGarmin()

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f' — {detail}' if detail and not ok else ''}")
    if not ok:
        failures.append(name)


# --- A stand-in for Google's token endpoint --------------------------------

google = {"email": ME, "email_verified": True, "aud": GOOGLE_CLIENT, "nonce": None}


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


def google_token_endpoint(request: httpx.Request) -> httpx.Response:
    form = parse_qs(request.content.decode())
    assert form["client_secret"] == ["google-secret"]
    assert form["redirect_uri"] == [f"{PUBLIC}/oauth/google/callback"]
    assert form["code_verifier"][0]
    claims = {
        "iss": "https://accounts.google.com",
        "aud": google["aud"],
        "exp": time.time() + 300,
        "nonce": google["nonce"],
        "email": google["email"],
        "email_verified": google["email_verified"],
        "sub": "1234",
    }
    return httpx.Response(200, json={"id_token": _jwt(claims), "access_token": "x"})


server._oauth_provider._google_transport = httpx.MockTransport(google_token_endpoint)


# --- The flow, as Claude drives it ------------------------------------------


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def mcp_post(client: TestClient, token: str | None, body: dict, session_id: str | None = None):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["mcp-session-id"] = session_id
        headers["mcp-protocol-version"] = "2025-06-18"
    return client.post("/mcp", headers=headers, json=body)


def rpc_result(response) -> dict:
    """Streamable HTTP answers with SSE; pull out the JSON-RPC message."""
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return response.json()


def sign_in(client: TestClient, client_id: str, challenge: str, state: str = "claude-state"):
    """/authorize → Google → back here. Returns the callback response."""
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "garmin",
            "resource": f"{PUBLIC}/mcp",
        },
    )
    assert r.status_code == 302, (r.status_code, r.text)
    to_google = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(to_google.query).items()}
    google["nonce"] = q["nonce"]
    return to_google, q, client.get("/oauth/google/callback", params={"state": q["state"], "code": "g-code"})


def consent_token(page: str) -> str:
    return re.search(r'name=consent value="([^"]+)"', page).group(1)


def main() -> int:
    app = remote.app(server.mcp, server._oauth_provider)
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as client:
        print("\ndiscovery")
        r = mcp_post(client, None, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        check("unauthenticated /mcp is 401", r.status_code == 401, str(r.status_code))
        check(
            "401 points at the resource metadata",
            f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"'
            in r.headers.get("www-authenticate", ""),
            r.headers.get("www-authenticate", ""),
        )
        prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
        check("resource is the /mcp URL", prm.get("resource") == f"{PUBLIC}/mcp", str(prm))
        check("authorization server is this server", prm.get("authorization_servers", [""])[0].rstrip("/") == PUBLIC)
        asm = client.get("/.well-known/oauth-authorization-server").json()
        check("DCR advertised", asm.get("registration_endpoint") == f"{PUBLIC}/register")
        check("S256 PKCE advertised", asm.get("code_challenge_methods_supported") == ["S256"])

        print("\nregistration")
        for bad in ("https://evil.example/cb", "http://localhost:3000/other", "http://evil.example:80/callback"):
            r = client.post("/register", json={"redirect_uris": [bad], "token_endpoint_auth_method": "none"})
            check(f"refuses redirect {bad}", r.status_code == 400 and "invalid_redirect_uri" in r.text, r.text)
        r = client.post(
            "/register",
            json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none", "client_name": "Claude <b>"},
        )
        check("registers Claude's callback", r.status_code == 201, r.text)
        client_id = r.json()["client_id"]
        r = client.post(
            "/register",
            json={"redirect_uris": ["http://localhost:53121/callback"], "token_endpoint_auth_method": "none"},
        )
        check("registers a Claude Code loopback callback", r.status_code == 201, r.text)

        print("\nsign-in")
        verifier, challenge = pkce()
        google["email"] = "stranger@example.com"
        to_google, q, r = sign_in(client, client_id, challenge)
        check("/authorize sends the browser to Google", to_google.netloc == "accounts.google.com")
        check("Google request uses PKCE and a nonce", q.get("code_challenge_method") == "S256" and bool(q.get("nonce")))
        check("a Google account not on the list is refused", r.status_code == 403, str(r.status_code))
        check("refusal page doesn't leak the address", "stranger" not in r.text)

        google.update(email=ME, email_verified=False)
        _, _, r = sign_in(client, client_id, challenge)
        check("an unverified email is refused", r.status_code == 403, str(r.status_code))
        google["email_verified"] = True

        google["aud"] = "someone-elses-client"
        _, _, r = sign_in(client, client_id, challenge)
        check("an ID token for another client is refused", r.status_code == 403, str(r.status_code))
        google["aud"] = GOOGLE_CLIENT

        _, q, r = sign_in(client, client_id, challenge)
        check("allowed account reaches the consent page", r.status_code == 200, r.text[:200])
        check("consent page names where the code goes", "claude.ai" in r.text)
        check("consent page escapes the client name", "Claude &lt;b&gt;" in r.text)
        check("consent page can't be framed", "frame-ancestors 'none'" in r.headers.get("content-security-policy", ""))
        r2 = client.get("/oauth/google/callback", params={"state": q["state"], "code": "g-code"})
        check("Google's state can't be replayed", r2.status_code == 400, str(r2.status_code))

        r = client.post("/oauth/consent", data={"consent": consent_token(r.text), "decision": "deny"})
        denied = parse_qs(urlparse(r.headers.get("location", "")).query)
        check("deny returns access_denied to Claude", denied.get("error") == ["access_denied"], str(denied))
        check("deny keeps Claude's state", denied.get("state") == ["claude-state"])

        _, _, r = sign_in(client, client_id, challenge)
        token = consent_token(r.text)
        r = client.post("/oauth/consent", data={"consent": token, "decision": "approve"})
        back = urlparse(r.headers.get("location", ""))
        got = parse_qs(back.query)
        check("approve redirects to Claude's callback", back._replace(query="").geturl() == CALLBACK, back.geturl())
        check("approve returns a code and Claude's state", "code" in got and got.get("state") == ["claude-state"])
        r = client.post("/oauth/consent", data={"consent": token, "decision": "approve"})
        check("a consent can't be approved twice", r.status_code == 400, str(r.status_code))
        code = got["code"][0]

        print("\ntokens")
        exchange = {"grant_type": "authorization_code", "code": code, "client_id": client_id, "redirect_uri": CALLBACK}
        r = client.post("/token", data={**exchange, "code_verifier": "wrong" * 10})
        check("wrong PKCE verifier is refused", r.status_code == 400, r.text)
        r = client.post("/token", data={**exchange, "code_verifier": verifier})
        check("code exchanges for tokens", r.status_code == 200, r.text)
        tokens = r.json()
        check("access token lasts an hour", tokens.get("expires_in") == 3600)
        r = client.post("/token", data={**exchange, "code_verifier": verifier})
        check("a code can't be used twice", r.status_code == 400, r.text)

        stored = (STATE_DIR / "oauth.json").read_text()
        check("tokens are stored hashed, never raw", tokens["access_token"] not in stored and tokens["refresh_token"] not in stored)
        check("state file is 0600", (STATE_DIR / "oauth.json").stat().st_mode & 0o777 == 0o600)

        print("\nMCP over HTTP")
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        }
        r = mcp_post(client, "not-a-real-token", init)
        check("a made-up token is 401", r.status_code == 401, str(r.status_code))
        r = mcp_post(client, tokens["access_token"], init)
        check("initialize succeeds with the token", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        sid = r.headers.get("mcp-session-id")
        mcp_post(client, tokens["access_token"], {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        r = mcp_post(client, tokens["access_token"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
        names = {t["name"] for t in rpc_result(r).get("result", {}).get("tools", [])}
        check("tools are listed", "get_activities" in names and "create_workout" in names, str(names))
        r = mcp_post(
            client,
            tokens["access_token"],
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_daily_summary", "arguments": {"date": "today"}}},
            sid,
        )
        body = json.dumps(rpc_result(r))
        check("a tool call reaches the (stubbed) Garmin account", "12345" in body, body[:300])

        r = client.post(
            "/mcp",
            headers={"Host": "evil.example", "Authorization": f"Bearer {tokens['access_token']}",
                     "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json=init,
        )
        check("requests addressed to another host are refused", r.status_code in (400, 421), str(r.status_code))

        print("\nrefresh")
        refresh = {"grant_type": "refresh_token", "client_id": client_id}
        r = client.post("/token", data={**refresh, "refresh_token": tokens["refresh_token"]})
        check("refresh issues new tokens", r.status_code == 200, r.text)
        rotated = r.json()
        check("refresh token rotates", rotated.get("refresh_token") not in (None, tokens["refresh_token"]))
        r = mcp_post(client, rotated["access_token"], init)
        check("the new access token works", r.status_code == 200, str(r.status_code))

        r = client.post("/token", data={**refresh, "refresh_token": tokens["refresh_token"]})
        check("a rotated refresh token is refused", r.status_code == 400 and "invalid_grant" in r.text, r.text)
        r = mcp_post(client, rotated["access_token"], init)
        check("...but a near-simultaneous retry doesn't end the grant", r.status_code == 200, str(r.status_code))

        # Later reuse can only mean a copied token.
        store = server._oauth_provider.store
        with store.edit() as data:
            for entry in data["rotated"].values():
                entry["rotated_at"] -= 120
        r = client.post("/token", data={**refresh, "refresh_token": tokens["refresh_token"]})
        check("a rotated refresh token reused later is refused", r.status_code == 400, r.text)
        r = mcp_post(client, rotated["access_token"], init)
        check("...and reusing it revokes the whole grant", r.status_code == 401, str(r.status_code))
        r = client.post("/token", data={**refresh, "refresh_token": rotated["refresh_token"]})
        check("...including the newest refresh token", r.status_code == 400, r.text)

        print("\nallowlist")
        _, _, r = sign_in(client, client_id, challenge)
        r = client.post("/oauth/consent", data={"consent": consent_token(r.text), "decision": "approve"})
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        fresh = client.post("/token", data={**exchange, "code": code, "code_verifier": verifier}).json()
        provider = server._oauth_provider
        original = provider.config
        provider.config = replace(original, allowed_emails=frozenset({"other@example.com"}))
        r = mcp_post(client, fresh["access_token"], init)
        check("removing an address cuts off its live tokens", r.status_code == 401, str(r.status_code))
        provider.config = original

    print("\nconfiguration")
    base = {k: v for k, v in os.environ.items() if k.startswith("GARMIN_MCP_")}
    for name in ("GARMIN_MCP_GOOGLE_CLIENT_ID", "GARMIN_MCP_ALLOWED_EMAILS"):
        try:
            OAuthConfig.from_env({**base, name: ""})
            check(f"missing {name} is refused", False)
        except Exception as exc:
            check(f"missing {name} is refused", name in str(exc), str(exc))
    for url in ("http://mini.example.ts.net", "https://mini.example.ts.net/mcp"):
        try:
            OAuthConfig.from_env({**base, "GARMIN_MCP_PUBLIC_URL": url})
            check(f"public URL {url} is refused", False)
        except Exception:
            check(f"public URL {url} is refused", True)

    saved = dict(os.environ)
    os.environ.update(GARMIN_MCP_AUTH="none", GARMIN_MCP_HOST="0.0.0.0")
    try:
        remote.server_options()
        check("auth can't be turned off on a public interface", False)
    except SystemExit:
        check("auth can't be turned off on a public interface", True)
    os.environ.update(GARMIN_MCP_HOST="127.0.0.1")
    check("auth can be turned off on loopback", remote.server_options() == ({}, None))
    os.environ.clear()
    os.environ.update(saved)

    print(f"\n{'all checks passed' if not failures else f'{len(failures)} FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
