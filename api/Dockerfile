# API image: FastAPI + LangGraph supervisor. The two MCP servers (travel_search, payments)
# run as stdio subprocesses of this container, exactly as with `uv run uvicorn` locally.
# Build context is the repo root:  docker build -f api/Dockerfile .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY agents ./agents
COPY api ./api
COPY config ./config
COPY controls ./controls
COPY mcp_servers ./mcp_servers
# Only to create DEMO_USERS entries on a host without Python: docker compose run --rm api python scripts/hash_password.py <name>
COPY scripts/hash_password.py ./scripts/hash_password.py
RUN uv sync --locked --no-dev

# Unprivileged user; var/ holds the audit log and the SQLite files lab.db (checkpoints, run
# history) and payments.db (the payments ledger), mounted as one volume by docker-compose.
RUN useradd --create-home --uid 10001 lab \
    && mkdir -p /app/var \
    && chown -R lab:lab /app/var
USER lab

ENV PATH="/app/.venv/bin:${PATH}" \
    AUDIT_LOG_PATH=/app/var/audit.jsonl \
    LAB_DB_PATH=/app/var/lab.db \
    PAYMENTS_DB_PATH=/app/var/payments.db \
    WALLET_DB_PATH=/app/var/wallet.db

EXPOSE 8000
# --no-server-header: do not advertise the server software; --proxy-headers for HTTPS behind a proxy.
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--proxy-headers"]
