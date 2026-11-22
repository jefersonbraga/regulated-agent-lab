# API image: FastAPI + LangGraph supervisor. The MCP servers (travel_search, payments, wallet)
# run as stdio subprocesses of this container, exactly as with `uv run uvicorn` locally.
# Build context is backend/:  docker build backend
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY config ./config
# Only to create DEMO_USERS entries on a host without Python: docker compose run --rm api ral-hash-password <name>
RUN uv sync --locked --no-dev

# Unprivileged user; var/ holds the audit log and the SQLite files lab.db (checkpoints, run
# history), payments.db (the payments ledger) and wallet.db, mounted as one volume by compose.
RUN useradd --create-home --uid 10001 lab \
    && mkdir -p /app/var \
    && chown -R lab:lab /app/var
USER lab

ENV PATH="/app/.venv/bin:${PATH}" \
    RAL_CONFIG_DIR=/app/config \
    AUDIT_LOG_PATH=/app/var/audit.jsonl \
    LAB_DB_PATH=/app/var/lab.db \
    PAYMENTS_DB_PATH=/app/var/payments.db \
    WALLET_DB_PATH=/app/var/wallet.db

EXPOSE 8000
# --no-server-header: do not advertise the server software; --proxy-headers for HTTPS behind a proxy.
CMD ["uvicorn", "regulated_agent_lab.api.server:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--proxy-headers"]
