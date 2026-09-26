"""OAuth for the HTTP transport: Claude gets a token, Google says who you are.

The stdio server needs none of this. Over HTTP the server is reachable by URL,
so every MCP request must carry a bearer token this server issued, and the only
way to get one is to sign in with a Google account on the allowlist and then
approve the client on a consent page served from here.

The MCP SDK implements the OAuth endpoints themselves (metadata, dynamic client
registration, /authorize, /token with PKCE, /revoke). This module is the
provider behind them:

- /authorize sends the browser to Google instead of showing a password form.
  No password is held here, and Google's 2FA protects the sign-in.
- Google's answer must name an allowed, verified email address. Anything else
  is refused before a code is ever issued.
- A consent page then shows which client is asking and where the code will be
  sent, so a registration you didn't make can't quietly get a token.
- Only SHA-256 hashes of tokens are written to disk. Refresh tokens rotate on
  every use, and presenting a rotated one revokes the whole grant, since that
  only happens if a token was copied.

The Garmin password and session never pass through any of this.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import logging
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode, urlparse

try:  # mcp >= 2.2 ships httpx2; older installs have httpx
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger(__name__)

SCOPE = "garmin"

# Where Claude's hosted apps (web, desktop, mobile) send the code. Claude Code
# uses a loopback address on a port that changes per session instead.
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

GOOGLE_CALLBACK_PATH = "/oauth/google/callback"
CONSENT_PATH = "/oauth/consent"

ACCESS_TOKEN_SECONDS = 60 * 60
REFRESH_TOKEN_SECONDS = 30 * 24 * 60 * 60
CODE_SECONDS = 5 * 60
SIGN_IN_SECONDS = 10 * 60
MAX_CLIENTS = 50
MAX_PENDING_SIGN_INS = 20
# Claude can refresh proactively and on a 401 at nearly the same moment. A
# rotated token presented again this soon is refused, but not treated as theft.
REUSE_GRACE_SECONDS = 60

DEFAULT_STATE_FILE = Path.home() / ".garmin-mcp" / "oauth.json"


def state_file_from_env(env: Any) -> Path:
    """Next to the Garmin token cache unless set, so one volume holds both."""
    if env.get("GARMIN_MCP_OAUTH_STATE"):
        return Path(env["GARMIN_MCP_OAUTH_STATE"])
    if env.get("GARMIN_MCP_TOKENS"):
        return Path(env["GARMIN_MCP_TOKENS"]).parent / "oauth.json"
    return DEFAULT_STATE_FILE


class ConfigError(RuntimeError):
    """The HTTP transport is missing something it needs to start safely."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _is_loopback_callback(uri: str) -> bool:
    parsed = urlparse(uri)
    return (
        parsed.scheme == "http"
        and parsed.hostname in LOOPBACK_HOSTS
        and parsed.path == "/callback"
        and not parsed.query
        and not parsed.fragment
    )


@dataclass(frozen=True)
class OAuthConfig:
    public_url: str
    google_client_id: str
    google_client_secret: str
    allowed_emails: frozenset[str]
    state_file: Path = DEFAULT_STATE_FILE
    extra_redirect_uris: frozenset[str] = frozenset()

    @property
    def resource_url(self) -> str:
        return f"{self.public_url}/mcp"

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.public_url}{GOOGLE_CALLBACK_PATH}"

    def redirect_allowed(self, uri: str) -> bool:
        return uri == CLAUDE_CALLBACK or uri in self.extra_redirect_uris or _is_loopback_callback(uri)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "OAuthConfig":
        env = os.environ if env is None else env
        get = lambda name: (env.get(name) or "").strip()  # noqa: E731

        missing = [
            name
            for name in (
                "GARMIN_MCP_PUBLIC_URL",
                "GARMIN_MCP_GOOGLE_CLIENT_ID",
                "GARMIN_MCP_GOOGLE_CLIENT_SECRET",
                "GARMIN_MCP_ALLOWED_EMAILS",
            )
            if not get(name)
        ]
        if missing:
            raise ConfigError(
                "The HTTP transport requires OAuth sign-in, and these are not set: "
                + ", ".join(missing)
                + ". See SELF-HOSTING.md."
            )

        public_url = get("GARMIN_MCP_PUBLIC_URL").rstrip("/")
        parsed = urlparse(public_url)
        local = parsed.hostname in LOOPBACK_HOSTS
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ConfigError(
                f"GARMIN_MCP_PUBLIC_URL must be https:// (got {public_url!r}); "
                "tokens must never cross the network in the clear."
            )
        if parsed.path or parsed.query:
            raise ConfigError(
                "GARMIN_MCP_PUBLIC_URL is the bare origin, e.g. https://mini.example.ts.net; "
                "the MCP endpoint is served at /mcp under it."
            )

        emails = frozenset(
            e.strip().lower() for e in get("GARMIN_MCP_ALLOWED_EMAILS").split(",") if e.strip()
        )
        extra = frozenset(u.strip() for u in get("GARMIN_MCP_EXTRA_REDIRECT_URIS").split(",") if u.strip())
        return cls(
            public_url=public_url,
            google_client_id=get("GARMIN_MCP_GOOGLE_CLIENT_ID"),
            google_client_secret=get("GARMIN_MCP_GOOGLE_CLIENT_SECRET"),
            allowed_emails=emails,
            state_file=state_file_from_env(env),
            extra_redirect_uris=extra,
        )


class Store:
    """Registered clients and token hashes, in one small JSON file.

    Read from disk on every lookup, so `python -m garmin_mcp.oauth revoke-all`
    takes effect on a running server immediately. The traffic is one person's,
    so the file stays tiny and the reads are cheap.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {}
        for key in ("clients", "access", "refresh", "rotated"):
            data.setdefault(key, {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    @contextlib.contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            data = self._load()
            yield data
            _prune(data)
            self._save(data)


def _prune(data: dict[str, Any]) -> None:
    now = time.time()
    for key in ("access", "refresh", "rotated"):
        data[key] = {h: t for h, t in data[key].items() if t["expires_at"] > now}
    # Claude registers a fresh client on each new connection. Keep the most
    # recent ones, and never drop one that still holds a live grant.
    clients = data["clients"]
    if len(clients) > MAX_CLIENTS:
        live = {t["client_id"] for t in data["refresh"].values()}
        stale = sorted(
            (c for c in clients if c not in live),
            key=lambda c: clients[c].get("client_id_issued_at") or 0,
        )
        for client_id in stale[: len(clients) - MAX_CLIENTS]:
            del clients[client_id]


@dataclass
class SignIn:
    """A browser that has left for Google and not come back yet."""

    client_id: str
    params: AuthorizationParams
    google_verifier: str
    nonce: str
    started: float = field(default_factory=time.time)
    email: str | None = None


class GoogleOAuthProvider:
    """The SDK's OAuthAuthorizationServerProvider, backed by Google sign-in."""

    def __init__(self, config: OAuthConfig, *, google_transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.store = Store(config.state_file)
        self._google_transport = google_transport
        self._lock = threading.Lock()
        self._sign_ins: dict[str, SignIn] = {}  # keyed by the state sent to Google
        self._consents: dict[str, SignIn] = {}  # keyed by the consent form's token
        self._codes: dict[str, AuthorizationCode] = {}

    # --- Clients -----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        stored = self.store.read()["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(stored) if stored else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        refused = [u for u in uris if not self.config.redirect_allowed(u)]
        if not uris or refused:
            log.warning("Refused client registration with redirect URIs %r", uris)
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description=(
                    "This server only issues codes to Claude "
                    f"({CLAUDE_CALLBACK}) or a loopback /callback."
                ),
            )
        with self.store.edit() as data:
            data["clients"][client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
        log.info("Registered client %r (%s)", client_info.client_name, client_info.client_id)

    # --- Authorization -----------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.config.resource_url:
            raise AuthorizeError(error="invalid_target", error_description="Unknown resource.")
        if not self.config.redirect_allowed(str(params.redirect_uri)):
            raise AuthorizeError(error="unauthorized_client", error_description="Redirect URI not allowed.")

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        nonce = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        with self._lock:
            self._expire_sign_ins()
            # Registration is open, so anyone can start a sign-in. Keep only the
            # newest few rather than let abandoned ones pile up.
            while len(self._sign_ins) >= MAX_PENDING_SIGN_INS:
                del self._sign_ins[next(iter(self._sign_ins))]
            self._sign_ins[state] = SignIn(client.client_id, params, verifier, nonce)

        return GOOGLE_AUTHORIZE_URL + "?" + urlencode(
            {
                "client_id": self.config.google_client_id,
                "redirect_uri": self.config.google_redirect_uri,
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )

    def _expire_sign_ins(self) -> None:
        cutoff = time.time() - SIGN_IN_SECONDS
        for pending in (self._sign_ins, self._consents):
            for key in [k for k, v in pending.items() if v.started < cutoff]:
                del pending[key]
        for code in [c for c, v in self._codes.items() if v.expires_at < time.time()]:
            del self._codes[code]

    async def google_callback(self, request: Request) -> Response:
        """Google sends the browser back here. Check who signed in, then ask for consent."""
        state = request.query_params.get("state", "")
        with self._lock:
            self._expire_sign_ins()
            sign_in = self._sign_ins.pop(state, None)
        if sign_in is None:
            return _page("Sign-in expired", "That sign-in link is no longer valid. Start again from Claude.", 400)
        if request.query_params.get("error"):
            return self._back_to_client(sign_in, error="access_denied")

        try:
            email = await self._verified_email(request.query_params.get("code", ""), sign_in)
        except PermissionError as exc:
            log.warning("Refused sign-in: %s", exc)
            return _page("Not allowed", "That Google account can't use this server.", 403)
        except Exception:  # noqa: BLE001 - never show Google's raw response
            log.exception("Google sign-in failed")
            return _page("Sign-in failed", "Google sign-in didn't complete. Start again from Claude.", 502)

        sign_in.email = email
        consent_token = secrets.token_urlsafe(32)
        with self._lock:
            self._consents[consent_token] = sign_in
        client = await self.get_client(sign_in.client_id)
        return self._consent_page(consent_token, sign_in, client)

    async def _verified_email(self, code: str, sign_in: SignIn) -> str:
        async with httpx.AsyncClient(transport=self._google_transport, timeout=10) as http:
            response = await http.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.config.google_client_id,
                    "client_secret": self.config.google_client_secret,
                    "redirect_uri": self.config.google_redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": sign_in.google_verifier,
                },
            )
        response.raise_for_status()
        # The ID token came straight from Google's token endpoint over TLS,
        # authenticated with our client secret, so OIDC Core 3.1.3.7 lets the TLS
        # connection stand in for the signature check. The claims still are checked.
        claims = _jwt_claims(response.json()["id_token"])
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise PermissionError(f"unexpected issuer {claims.get('iss')!r}")
        if claims.get("aud") != self.config.google_client_id:
            raise PermissionError("ID token was issued to a different client")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < time.time():
            raise PermissionError("ID token has expired")
        if not secrets.compare_digest(str(claims.get("nonce", "")), sign_in.nonce):
            raise PermissionError("nonce mismatch")
        email = str(claims.get("email", "")).lower()
        if claims.get("email_verified") is not True:
            raise PermissionError(f"email {email!r} is not verified with Google")
        if email not in self.config.allowed_emails:
            raise PermissionError(f"{email!r} is not in GARMIN_MCP_ALLOWED_EMAILS")
        return email

    def _consent_page(
        self, consent_token: str, sign_in: SignIn, client: OAuthClientInformationFull | None
    ) -> Response:
        redirect = urlparse(str(sign_in.params.redirect_uri))
        name = html.escape((client.client_name if client else None) or "An unnamed client")
        where = html.escape(redirect.netloc)
        warning = ""
        if redirect.hostname in LOOPBACK_HOSTS:
            warning = (
                "<p class=warn>The code goes to a program on your own computer. Approve this only "
                "if you just started a sign-in from Claude Code on this machine.</p>"
            )
        body = f"""
<p>Signed in as <b>{html.escape(sign_in.email or "")}</b>.</p>
<p><b>{name}</b> is asking to read your Garmin data and add workouts to your calendar.
The approval will be sent to <b>{where}</b>.</p>
{warning}
<form method=post action="{CONSENT_PATH}">
<input type=hidden name=consent value="{html.escape(consent_token)}">
<button name=decision value=approve>Approve</button>
<button name=decision value=deny>Deny</button>
</form>"""
        return _page("Allow access to your Garmin data?", body, 200, raw=True)

    async def consent(self, request: Request) -> Response:
        form = await request.form()
        with self._lock:
            self._expire_sign_ins()
            sign_in = self._consents.pop(str(form.get("consent", "")), None)
        if sign_in is None or sign_in.email is None:
            return _page("Approval expired", "That approval is no longer valid. Start again from Claude.", 400)
        if form.get("decision") != "approve":
            log.info("%s denied access to client %s", sign_in.email, sign_in.client_id)
            return self._back_to_client(sign_in, error="access_denied")

        code = secrets.token_urlsafe(32)
        params = sign_in.params
        with self._lock:
            self._codes[code] = AuthorizationCode(
                code=code,
                scopes=params.scopes or [SCOPE],
                expires_at=time.time() + CODE_SECONDS,
                client_id=sign_in.client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=self.config.resource_url,
                subject=sign_in.email,
            )
        log.info("%s approved client %s", sign_in.email, sign_in.client_id)
        return self._back_to_client(sign_in, code=code)

    @staticmethod
    def _back_to_client(sign_in: SignIn, **result: str) -> Response:
        url = construct_redirect_uri(str(sign_in.params.redirect_uri), state=sign_in.params.state, **result)
        return RedirectResponse(url, status_code=303)

    # --- Tokens ------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self._lock:
            return self._codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self._lock:
            if self._codes.pop(authorization_code.code, None) is None:
                raise TokenError(error="invalid_grant", error_description="Code already used.")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject or "",
                           family=secrets.token_hex(16))

    def _issue(self, client_id: str, scopes: list[str], email: str, family: str) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        grant = {"client_id": client_id, "scopes": scopes, "email": email, "family": family}
        with self.store.edit() as data:
            data["access"][_hash(access)] = {**grant, "expires_at": now + ACCESS_TOKEN_SECONDS}
            data["refresh"][_hash(refresh)] = {**grant, "expires_at": now + REFRESH_TOKEN_SECONDS}
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_SECONDS,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        digest = _hash(refresh_token)
        with self.store.edit() as data:
            reused = data["rotated"].get(digest)
            if reused and time.time() - reused.get("rotated_at", 0) < REUSE_GRACE_SECONDS:
                return None
            if reused:
                # A refresh token is only ever presented twice if someone else
                # has a copy. End the whole grant rather than guess which is real.
                log.warning("Rotated refresh token reused; revoking grant %s", reused["family"])
                _revoke_family(data, reused["family"])
                return None
            stored = data["refresh"].get(digest)
        if not stored or stored["email"] not in self.config.allowed_emails:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=stored["client_id"],
            scopes=stored["scopes"],
            expires_at=stored["expires_at"],
            resource=self.config.resource_url,
            subject=stored["email"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        digest = _hash(refresh_token.token)
        with self.store.edit() as data:
            stored = data["refresh"].pop(digest, None)
            if stored is None:
                raise TokenError(error="invalid_grant", error_description="Refresh token is no longer valid.")
            data["rotated"][digest] = {
                "family": stored["family"],
                "expires_at": stored["expires_at"],
                "rotated_at": time.time(),
            }
        return self._issue(client.client_id, scopes or refresh_token.scopes, stored["email"], stored["family"])

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = self.store.read()["access"].get(_hash(token))
        if not stored or stored["expires_at"] < time.time():
            return None
        # Taking an address off the allowlist cuts it off at once, without
        # waiting for its tokens to expire.
        if stored["email"] not in self.config.allowed_emails:
            return None
        return AccessToken(
            token=token,
            client_id=stored["client_id"],
            scopes=stored["scopes"],
            expires_at=stored["expires_at"],
            resource=self.config.resource_url,
            subject=stored["email"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        digest = _hash(token.token)
        with self.store.edit() as data:
            stored = data["access"].get(digest) or data["refresh"].get(digest)
            if stored:
                _revoke_family(data, stored["family"])


def _revoke_family(data: dict[str, Any], family: str) -> None:
    for key in ("access", "refresh", "rotated"):
        data[key] = {h: t for h, t in data[key].items() if t.get("family") != family}


def _jwt_claims(jwt: str) -> dict[str, Any]:
    payload = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


_STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem}"
    "button{font:inherit;padding:.5rem 1.25rem;margin-right:.5rem}"
    ".warn{background:#fff3cd;padding:.75rem;border-radius:.25rem}"
)


def _page(title: str, body: str, status: int, raw: bool = False) -> HTMLResponse:
    content = body if raw else f"<p>{html.escape(body)}</p>"
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style>"
        f"<h1>{html.escape(title)}</h1>{content}",
        status_code=status,
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        },
    )


def main(argv: list[str] | None = None) -> int:
    """Inspect or revoke what this server has handed out.

        python -m garmin_mcp.oauth status
        python -m garmin_mcp.oauth revoke-all
    """
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "status"
    path = state_file_from_env(os.environ)
    store = Store(path)

    if command == "status":
        data = store.read()
        _prune(data)
        grants: dict[str, dict[str, Any]] = {}
        for t in data["refresh"].values():
            grants[t["family"]] = t
        print(f"{path}: {len(data['clients'])} registered clients, {len(grants)} active grants")
        for t in grants.values():
            client = data["clients"].get(t["client_id"], {})
            expires = time.strftime("%Y-%m-%d", time.localtime(t["expires_at"]))
            print(f"  {t['email']} via {client.get('client_name') or t['client_id']} (until {expires})")
        return 0
    if command == "revoke-all":
        with store.edit() as data:
            data.clear()
            data.update(clients={}, access={}, refresh={}, rotated={})
        print("Revoked every token and client registration. Each Claude client will ask you to sign in again.")
        return 0
    print(main.__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
