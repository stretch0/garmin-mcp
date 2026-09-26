# Self-hosting over HTTP

The normal install needs none of this: Claude Desktop starts the server as a
local process and talks to it over stdio. This page is for running it as a
long-lived service instead, on a machine at home, so every Claude client on
your account can use it: web, desktop, mobile and Claude Code.

It's for **one person's own Garmin account**, on a home connection. Garmin's
sign-in sits behind bot protection that blocks datacenter IP addresses, so a
cloud host or a VPN exit won't work. A home server does.

## How access is protected

Claude's servers make the connection, not your device, so the server has to be
reachable at a public HTTPS address. Access is therefore gated by OAuth, and
the server refuses to start in HTTP mode without it:

1. You add the server's URL to Claude as a custom connector.
2. Claude registers itself and sends you to sign in. The sign-in is Google's,
   so no password is held here and your Google 2FA applies.
3. Only the addresses in `GARMIN_MCP_ALLOWED_EMAILS` get past that point.
4. A consent page then shows which client is asking and where the approval is
   going, and you approve it.
5. Claude gets a one-hour access token and a refresh token, and sends the
   access token on every request.

Details worth knowing:

- Tokens are stored only as SHA-256 hashes, in `oauth.json` next to the Garmin
  session. Refresh tokens rotate on every use, and a reused one revokes the
  whole grant, because that only happens if a token was copied.
- Codes are only ever sent to Claude's callback
  (`https://claude.ai/api/mcp/auth_callback`) or to a loopback `/callback`
  for Claude Code. Registrations asking for anything else are refused.
- Requests addressed to any other hostname are rejected.
- Removing an address from `GARMIN_MCP_ALLOWED_EMAILS` cuts off its tokens at
  once.
- Your Garmin password and session never leave the server. OAuth only decides
  who may call the tools.
- Anyone signed in to your Claude account can use the connector, like any
  other connector, so keep 2FA on that account too.

## Setup

### 1. A public HTTPS address

Use anything that terminates TLS and forwards to the container's port without
opening ports on your router. With Tailscale Funnel on the host:

```bash
tailscale funnel --bg 8000
```

That serves `https://<machine>.<tailnet>.ts.net`. That origin, with no path, is
your `GARMIN_MCP_PUBLIC_URL`. Cloudflare Tunnel works the same way.

### 2. A Google OAuth client

In the [Google Cloud console](https://console.cloud.google.com/):

1. **APIs & Services → OAuth consent screen**: choose *External*, fill in the
   app name, and add your own address under *Test users*. Only the `openid`
   and `email` scopes are used, so the app never needs Google's review.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   choose *Web application*, and add this authorized redirect URI:
   `https://<your public address>/oauth/google/callback`
3. Copy the client ID and client secret.

### 3. Configuration

| Variable | |
| --- | --- |
| `GARMIN_MCP_TRANSPORT` | `http`. The Docker image sets it already. |
| `GARMIN_MCP_PUBLIC_URL` | The public origin from step 1, e.g. `https://mini.example.ts.net` |
| `GARMIN_MCP_GOOGLE_CLIENT_ID` | From step 2 |
| `GARMIN_MCP_GOOGLE_CLIENT_SECRET` | From step 2 |
| `GARMIN_MCP_ALLOWED_EMAILS` | Comma-separated Google addresses allowed in. Usually just yours. |
| `GARMIN_MCP_HOST` / `GARMIN_MCP_PORT` | Where to listen. Defaults to `127.0.0.1:8000`; the image uses `0.0.0.0` |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Optional, as for the local install: lets it sign in to Garmin again by itself when the session expires |

`GARMIN_MCP_AUTH=none` turns OAuth off, but only when the server listens on a
loopback address. It's meant for trying things out locally.

### 4. Run it

```yaml
services:
  garmin-mcp:
    image: ghcr.io/<owner>/garmin-mcp:latest
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - ./garmin-data:/data
    env_file: .env
    restart: unless-stopped
```

Sign in to Garmin once, interactively, so a multi-factor prompt can be
answered. The session is cached in the volume:

```bash
docker compose run --rm garmin-mcp python -m garmin_mcp.login
docker compose up -d
```

Or build the image yourself with `docker build -t garmin-mcp .`.

### 5. Connect Claude

- **Claude web, desktop and mobile:** Settings → Connectors → Add custom
  connector, with the URL `https://<your public address>/mcp`. Sign in when
  asked. It then works in every Claude app on your account.
- **Claude Code:**
  `claude mcp add --transport http garmin https://<your public address>/mcp`,
  then run `/mcp` to sign in.

## Managing access

```bash
docker compose exec garmin-mcp python -m garmin_mcp.oauth status      # who holds a grant
docker compose exec garmin-mcp python -m garmin_mcp.oauth revoke-all  # sign everything out
```

Both take effect on the running server. Every tool call is logged with the
address it was made for.
