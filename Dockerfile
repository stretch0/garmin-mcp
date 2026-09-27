# The server as a long-lived HTTP service, for running on a home server.
# The local install (Claude Desktop over stdio) needs none of this; see
# SELF-HOSTING.md for setup, including the Google sign-in it requires.
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 app \
    && mkdir /data && chown app /data && chmod 700 /data
WORKDIR /app

COPY requirements.txt ./
# The HTTP transport needs a newer mcp than the stdio floor in requirements.txt.
RUN pip install --no-cache-dir --only-binary :all: -r requirements.txt "mcp>=2.2,<3"

COPY garmin_mcp/ ./garmin_mcp/

# The Garmin session and the OAuth grants both live on the /data volume, so
# they survive a redeploy. Mount it; never bake either into an image.
ENV GARMIN_MCP_TRANSPORT=http \
    GARMIN_MCP_HOST=0.0.0.0 \
    GARMIN_MCP_PORT=8000 \
    GARMIN_MCP_TOKENS=/data/tokens.json \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
USER app
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/.well-known/oauth-authorization-server', timeout=4)"

CMD ["python", "-m", "garmin_mcp"]
