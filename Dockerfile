# Image for running bb-mcp as an HTTP server (Streamable HTTP on :8000/mcp).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

WORKDIR /app

# Install dependencies first so this layer is cached unless pyproject/lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the package itself.
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 app && chown -R app /app
USER app

EXPOSE 8000

CMD ["/app/.venv/bin/python", "-m", "bb_mcp.server"]
