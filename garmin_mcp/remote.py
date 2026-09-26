"""The opt-in HTTP transport, for running the server as a long-lived service.

stdio stays the default and needs nothing from this module. Setting
GARMIN_MCP_TRANSPORT=http serves MCP at /mcp instead, behind OAuth (see
oauth.py). The choice is read from the environment at import time because the
SDK takes its auth provider when the server object is built.

    GARMIN_MCP_TRANSPORT     stdio (default) or http
    GARMIN_MCP_HOST          interface to listen on, default 127.0.0.1
    GARMIN_MCP_PORT          default 8000
    GARMIN_MCP_AUTH          oauth (default) or none; none is refused unless
                             the server only listens on a loopback address
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def transport() -> str:
    value = os.environ.get("GARMIN_MCP_TRANSPORT", "stdio").strip().lower() or "stdio"
    if value not in {"stdio", "http"}:
        raise SystemExit(f"GARMIN_MCP_TRANSPORT must be 'stdio' or 'http', not {value!r}.")
    return value


def _host() -> str:
    return os.environ.get("GARMIN_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _port() -> int:
    return int(os.environ.get("GARMIN_MCP_PORT", "8000"))


def _auth_disabled() -> bool:
    mode = os.environ.get("GARMIN_MCP_AUTH", "oauth").strip().lower()
    if mode not in {"oauth", "none"}:
        raise SystemExit(f"GARMIN_MCP_AUTH must be 'oauth' or 'none', not {mode!r}.")
    if mode == "none" and _host() not in LOOPBACK:
        raise SystemExit(
            "GARMIN_MCP_AUTH=none is only allowed when GARMIN_MCP_HOST is a loopback "
            "address. Anyone who can reach this server could read your Garmin data."
        )
    return mode == "none"


def server_options() -> tuple[dict[str, Any], Any]:
    """Constructor arguments for MCPServer, and the OAuth provider if there is one."""
    if transport() != "http" or _auth_disabled():
        return {}, None

    try:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
        from mcp.server.mcpserver import MCPServer  # noqa: F401 - the version check
    except ImportError:
        raise SystemExit("The HTTP transport needs mcp >= 2.2: pip install -U 'mcp>=2.2'") from None

    from .oauth import SCOPE, ConfigError, GoogleOAuthProvider, OAuthConfig

    try:
        config = OAuthConfig.from_env()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None

    provider = GoogleOAuthProvider(config)
    auth = AuthSettings(
        issuer_url=config.public_url,
        resource_server_url=config.resource_url,
        validate_token_resource=True,
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    return {"auth": auth, "auth_server_provider": provider}, provider


def app(mcp: Any, provider: Any) -> Any:
    """The ASGI app: MCP at /mcp, plus the OAuth endpoints when auth is on."""
    from mcp.server.transport_security import TransportSecuritySettings

    from .oauth import CONSENT_PATH, GOOGLE_CALLBACK_PATH

    if provider is not None:
        mcp.custom_route(GOOGLE_CALLBACK_PATH, methods=["GET"])(provider.google_callback)
        mcp.custom_route(CONSENT_PATH, methods=["POST"])(provider.consent)

    # Only accept requests addressed to this server by name, which stops a web
    # page from reaching it through DNS rebinding.
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if provider is not None:
        public = urlparse(provider.config.public_url)
        hosts += [public.netloc, f"{public.hostname}:*"]
        origins += [provider.config.public_url, "https://claude.ai"]

    return mcp.streamable_http_app(
        host=_host(),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
        ),
    )


def run(mcp: Any, provider: Any) -> None:
    import uvicorn

    host, port = _host(), _port()
    log.info(
        "Serving MCP over HTTP on %s:%d%s",
        host,
        port,
        f" as {provider.config.resource_url}" if provider else " WITHOUT authentication (loopback only)",
    )
    uvicorn.run(app(mcp, provider), host=host, port=port, log_level="info")
