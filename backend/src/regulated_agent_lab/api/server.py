"""HTTP API over the supervisor graph: runs, human approval and the audit log.

    uv run uvicorn regulated_agent_lab.api.server:app --reload

``run_id`` (``uuid4().hex``) is the LangGraph ``thread_id``. A per-run lock serialises
approvals, so a double click resumes the graph once; later attempts get ``409``. The payments
server's idempotency key (= ``run_id``) is the second line of defence. Simulated data only.

The approval body may carry ``fare_ids`` (one offered fare per leg); ``POST /runs/{run_id}/quote``
prices such a selection without side effects, so the approver sees the exact total and hash
the decision will be bound to.

``POST /runs/stream`` and ``POST /runs/{run_id}/approval/stream`` do the same work and stream
live run events as server-sent events (one JSON object per ``data:`` line), ending with the
same ``RunView`` as ``GET /runs/{run_id}``. The graph runs in its own task under the per-run
lock, so a client that disconnects never cancels a run (or a charge) halfway. Events carry
node, agent and tool names, flags and statuses only: never tool args, results or the message.

Persistence (``agents.persistence``): the lifespan opens the SQLite file at ``LAB_DB_PATH``
(default ``backend/var/lab.db``) for the LangGraph checkpoints and the ``runs`` history, and
closes it on shutdown. A run paused for approval therefore survives a restart:
``GET /runs/{run_id}`` and the approval endpoints find it in the checkpoints (the per-run lock
is created on demand). ``GET /runs`` lists recent runs, newest first, with the request redacted.

After the payment (``services.payments.events`` and ``.reconcile``): the processor
is the source of truth. ``POST /webhooks/stripe`` is only the ingress for its signed events:
the raw body and the ``Stripe-Signature`` header go to the payments module, which holds the
endpoint secret, verifies, stores the event once in the payments database and matches it to
the ledger. This layer then mirrors the verified result into the run history (``settlement``)
and appends one ``processor`` audit entry per first-seen event. ``POST /reconcile`` reads every
test-mode capture back from the processor and reports discrepancies; processor-confirmed
settlements are mirrored too (``settlement_reconciled``), with one ``reconciliation_run``
audit entry per call.

Authentication (``regulated_agent_lab.api.auth``): every endpoint needs a signed session cookie from
``POST /auth/login`` except ``GET /health``, ``POST /auth/login``, ``POST /auth/logout`` and
``POST /webhooks/stripe`` (authenticated by the processor's signature instead). State-changing
requests must also carry an ``Origin`` from the allowed CORS origins (CSRF defence in depth on
top of ``SameSite=Strict``). The approver of a decision is the signed-in user, whatever the body
says; with ``REQUIRE_SEPARATE_APPROVER=1`` the requester of a run cannot approve it (403,
audited). Logins are rate limited per client IP, run creation, approval and payment checks per
user (in memory, per process: 429 with ``Retry-After``). Missing ``DEMO_USERS`` or
``SESSION_SECRET`` stops the app at startup unless ``API_AUTH_DISABLED=1`` (tests, local dev),
which skips sessions, origin checks and rate limits and keeps the approver the body names.

Hosted Checkout (``PAYMENTS_FLOW=checkout``, see ``agents.booking``): after approval the run
pauses with status ``awaiting_payment`` and ``RunView.checkout`` (the processor's payment page).
It resumes only on the processor's record: the signed ``checkout.session.completed`` or
``.expired`` webhook, or ``POST /runs/{run_id}/payment/refresh`` (reads the session back,
for local development without a webhook forwarder); ``POST /runs/{run_id}/payment/cancel``
closes the session. All three take the run's lock and the ledger moves with a conditional
update, so a webhook racing a refresh completes the run once.

Wallet (``PAYMENTS_FLOW=wallet`` or ``both``, see ``services.wallet``): the signed-in user
saves a card on the processor's hosted form (``POST /wallet/setup``, then
``POST /wallet/setup/complete`` on return, or the signed ``checkout.session.completed`` webhook in
``mode=setup``), sets a spending limit per booking (``POST /wallet/mandate``) and can remove the
card (``POST /wallet/card/remove``). This layer calls the wallet module and only ever sees brand,
last four digits and expiry. While a run awaits approval, ``RunView.payment_options`` says which
payment models the approver may choose and which is proposed (the wallet when a card is saved
and the total is within the limit); the choice travels with the decision (``payment_model``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, Field, StrictInt

from regulated_agent_lab import paths
from regulated_agent_lab.agents import booking
from regulated_agent_lab.agents.config import load_llm_config
from regulated_agent_lab.agents.graph import build_graph, initial_state
from regulated_agent_lab.agents.llm import LLM, build_llm, check_vllm_endpoint, load_mock_fixtures
from regulated_agent_lab.agents.persistence import (
    RunStore,
    RunSummary,
    Settlement,
    lab_db_path,
    open_checkpointer,
)
from regulated_agent_lab.agents.state import (
    SELECTED_RATIONALE,
    InvalidSelection,
    Quote,
    quote_for_selection,
)
from regulated_agent_lab.agents.tools import GatedToolClient
from regulated_agent_lab.api.auth import (
    COOKIE_NAME,
    AuthConfig,
    RateLimiter,
    User,
    retry_after_header,
)
from regulated_agent_lab.api.security_headers import SecurityHeadersMiddleware
from regulated_agent_lab.controls.audit_log import AuditEntry, AuditLog, VerifyResult
from regulated_agent_lab.controls.policy import ToolPolicy
from regulated_agent_lab.services.payments import checkout as checkouts
from regulated_agent_lab.services.payments import events as processor_events
from regulated_agent_lab.services.payments import processor
from regulated_agent_lab.services.payments import reconcile as reconciliation
from regulated_agent_lab.services.payments import server as payments_server
from regulated_agent_lab.services.wallet import processor as wallet_processor
from regulated_agent_lab.services.wallet import vault

# Vite dev server and `vite preview` of the production build.
ALLOWED_ORIGINS = ["http://localhost:5173", "http://localhost:4173"]
# Opt-in extra origins for local checks on other ports (comma-separated, exact origins only).
EXTRA_ORIGINS_ENV = "API_EXTRA_CORS_ORIGINS"
# The public UI (Checkout and card-form return URLs); its origin is allowed too.
PUBLIC_APP_URL_ENV = "PUBLIC_APP_URL"

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
# Seconds without an event before the stream sends an SSE comment (": keep-alive"). A slow model
# call (up to timeout_s x (1 + max_retries)) otherwise leaves the connection silent, and proxies
# in front of the API may close it before the final event arrives. SSE parsers skip comments.
SSE_KEEP_ALIVE_S = 15.0
SSE_KEEP_ALIVE = ": keep-alive\n\n"
STREAM_MODES = ["tasks", "custom"]
# Tool events written by GatedToolClient (agents.tools.emit_run_event); anything else is dropped.
TOOL_EVENT_TYPES = frozenset({"tool_call", "tool_result", "tool_error", "policy_denied"})
TOOL_EVENT_KEYS = frozenset({"type", "agent", "tool", "flagged", "reason"})


class CreateRun(BaseModel):
    message: str = Field(max_length=2000)


class Mode(BaseModel):
    """Which parts of a run are real. Fares and seat holds are always simulated."""

    timetable: str  # "simulated" | "live" (public timetable API)
    payments: str  # "simulated" | "stripe_test" (card processor in test mode, no money)
    # "agent" (simulated provider), or with the processor "checkout", "wallet" or "both".
    payments_flow: str = "agent"
    # The payment models an approver may choose ("agent"; or "checkout" and/or "wallet").
    payment_models: list[str] = ["agent"]


def current_mode() -> Mode:
    try:
        flow = booking.payments_flow()
        models = list(booking.payment_models())
    except ValueError:
        flow, models = "invalid", []
    return Mode(
        timetable="live" if os.environ.get("TRAVEL_SEARCH_SOURCE") == "live" else "simulated",
        payments=os.environ.get("PAYMENTS_PROVIDER", "simulated"),
        payments_flow=flow,
        payment_models=models,
    )


PaymentModel = Literal["checkout", "wallet", "agent"]


class ApprovalDecision(BaseModel):
    approved: bool
    # Kept for compatibility; with authentication on, the server sets it to the signed-in user.
    approver: str = Field(min_length=1, max_length=200)
    quote_hash: str = Field(max_length=128)
    # One offered fare id per leg; omitted means the recommended selection. A selection outside
    # the offer is not rejected here but by the graph, so the run records it and never pays.
    fare_ids: list[Annotated[str, Field(max_length=128)]] | None = Field(None, max_length=8)
    # How the approved total is paid; omitted: the proposal in ``RunView.payment_options``.
    payment_model: PaymentModel | None = None


class QuoteRequest(BaseModel):
    """A fare selection to price (one offered fare id per leg)."""

    fare_ids: list[Annotated[str, Field(max_length=128)]] = Field(max_length=8)


class Payment(BaseModel):
    """The captured payment as shown to the UI (integer cents)."""

    payment_id: str
    status: str
    amount_cents: int
    currency: str
    # The processor's public receipt page (no login needed); null for simulated payments.
    receipt_url: str | None = None


class Checkout(BaseModel):
    """A hosted Checkout Session waiting for the customer (status ``awaiting_payment``)."""

    session_id: str
    checkout_url: str
    expires_at: str
    amount_cents: int
    currency: str


class WalletCard(BaseModel):
    """A saved card as the user sees it: never the number, never the processor ids."""

    brand: str
    last4: str
    exp_month: int
    exp_year: int
    created_at: str | None = None


class PaymentOptions(BaseModel):
    """What the approver may choose while a run awaits approval (integer cents)."""

    models: list[str]
    # The model proposed: the wallet when it can pay this total, else the hosted Checkout.
    default: str | None
    # Whose wallet would pay (the requester; ``local-dev`` without authentication).
    payer: str
    card: WalletCard | None = None
    mandate_cents: int | None = None
    # Why the wallet cannot pay the recommended total now ("no_saved_card", "over_mandate", ...).
    wallet_unavailable: str | None = None


class WalletPayment(BaseModel):
    """The card and limit a completed wallet payment ran under (from the wallet's records)."""

    brand: str
    last4: str
    mandate_cents: int


class RunView(BaseModel):
    run_id: str
    status: str
    quote: dict | None
    interrupt: dict | None
    trajectory: list[str]
    payment: Payment | None = None
    flags: list[str] = []
    # What the card processor reported after the capture; null without a processor payment.
    settlement: Settlement | None = None
    # The processor's payment page while the run is awaiting payment; null otherwise.
    checkout: Checkout | None = None
    # "agent", "wallet" or "checkout" once the payment step ran; null before it.
    payment_model: str | None = None
    # Why a wallet payment fell back to the hosted Checkout; null otherwise.
    payment_fallback: str | None = None
    # While awaiting approval: the payment models to choose from; null otherwise.
    payment_options: PaymentOptions | None = None
    # A completed wallet payment: the card (brand, last four) and the limit it ran under.
    wallet_payment: WalletPayment | None = None


class WalletView(BaseModel):
    """GET /wallet: the signed-in user's saved card and spending limit per booking."""

    user: str
    # True when the wallet payment model is allowed (test-mode processor, PAYMENTS_FLOW).
    enabled: bool
    card: WalletCard | None = None
    mandate_cents: int
    currency: str = vault.CURRENCY
    min_mandate_cents: int = vault.MIN_MANDATE_CENTS
    max_mandate_cents: int = vault.MAX_MANDATE_CENTS
    # After POST /wallet/setup/complete: saved, pending, expired, refused or not_found.
    setup_state: str | None = None


class WalletSetup(BaseModel):
    """The processor's hosted card form (``mode=setup``): the browser goes there."""

    setup_url: str


class MandateUpdate(BaseModel):
    limit_cents: StrictInt = Field(ge=vault.MIN_MANDATE_CENTS, le=vault.MAX_MANDATE_CENTS)


class Me(BaseModel):
    """The signed-in user (``GET /auth/me``)."""

    name: str
    role: str
    auth_enabled: bool
    require_separate_approver: bool


class Login(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SettlementUpdate(BaseModel):
    run_id: str
    payment_id: str
    previous: Settlement | None
    settlement: Settlement


class ReconcileResult(BaseModel):
    report: reconciliation.ReconciliationReport
    settlement_updates: list[SettlementUpdate]


# Audit run_id for processor events that match no run, and for reconciliation summaries.
PROCESSOR_AUDIT_RUN = "processor"
RECONCILE_AUDIT_RUN = "reconcile"
# Audit run_id for logins and logouts.
AUTH_AUDIT_RUN = "auth"
# Audit run_id for a user's wallet changes (card saved or removed, limit changed).
WALLET_AUDIT_RUN = "wallet"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Checkout outcome that moved the ledger -> audit event name.
CHECKOUT_AUDIT = {
    checkouts.CAPTURED: "payment_confirmed",
    checkouts.EXPIRED: "checkout_expired",
    checkouts.CANCELED: "checkout_expired",
    checkouts.MISMATCH: "checkout_amount_mismatch",
}


def _event_audit(ingested: processor_events.Ingested) -> tuple[str, str, dict]:
    """(run_id, event, data) of the audit entry for a first-seen processor event. No PII:
    ids, type, integer cents and the outcome only."""
    ev = ingested.event
    data: dict[str, Any] = {
        "event_id": ev.event_id,
        "event_type": ev.type,
        "payment_id": ev.payment_id,
        "amount_cents": ev.amount_cents,
        "currency": ev.currency,
        "disposition": ingested.disposition,
    }
    if ingested.disposition == processor_events.LIVEMODE_REJECTED:
        return PROCESSOR_AUDIT_RUN, "livemode_event_rejected", data
    name = processor_events.HANDLED[ev.type][0]
    if ingested.disposition != processor_events.MATCHED or ingested.run_id is None:
        return PROCESSOR_AUDIT_RUN, name, data
    data["settlement"] = ingested.settlement
    if ingested.ledger_amount_cents is not None and ev.amount_cents is not None:
        same_currency = (ev.currency or "") == (ingested.ledger_currency or "").upper()
        if ev.type == "charge.refunded":
            data["refund_full"] = same_currency and ev.amount_cents >= ingested.ledger_amount_cents
        else:
            data["amount_matches_ledger"] = (
                same_currency and ev.amount_cents == ingested.ledger_amount_cents
            )
    return ingested.run_id, name, data


_LOG = logging.getLogger("uvicorn.error")


def _default_llm() -> LLM:
    cfg = load_llm_config(paths.llm_config_path())
    return build_llm(cfg, load_mock_fixtures(cfg.provider, os.environ.get("MOCK_FIXTURES")))


async def _probe_llm() -> None:
    """Warn at startup when the configured vLLM endpoint cannot serve runs; never blocks."""
    problem = await check_vllm_endpoint(load_llm_config(paths.llm_config_path()))
    if problem:
        _LOG.warning("LLM check: %s. Runs will fail until this is fixed.", problem)
    mode = current_mode()
    _LOG.info("Mode: timetable=%s, payments=%s", mode.timetable, mode.payments)


def _public_app_origin() -> str | None:
    """The origin of ``PUBLIC_APP_URL`` (the UI the processor sends customers back to), if any.

    That UI calls this API, so its origin is allowed without repeating it in
    ``API_EXTRA_CORS_ORIGINS``. Only an absolute http(s) URL counts; anything else is ignored
    here (the payment servers reject it when they build their return URLs).
    """
    parts = urlsplit((os.environ.get(PUBLIC_APP_URL_ENV) or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _allowed_origins() -> list[str]:
    extra = os.environ.get(EXTRA_ORIGINS_ENV, "")
    origins = [o.strip() for o in extra.split(",") if o.strip() and o.strip() != "*"]
    public = _public_app_origin()
    if public:
        origins.append(public)
    allowed = list(ALLOWED_ORIGINS)
    for origin in origins:
        if origin not in allowed:
            allowed.append(origin)
    return allowed


def _wallet_card(user: str) -> WalletCard | None:
    summary = vault.card_summary(user)
    return WalletCard(**summary) if summary is not None else None


def payment_options(values: dict, fare_ids: list[str] | None = None) -> PaymentOptions | None:
    """The payment models for a run awaiting approval, and the one to propose.

    The wallet is proposed when it can pay the total of ``fare_ids`` (the recommended selection
    when omitted): a saved, unexpired card and a total within the payer's mandate. No side
    effects (reads the wallet's records only).
    """
    try:
        models = booking.payment_models()
    except ValueError:
        return None
    quote, offer = values.get("quote"), values.get("offer")
    if quote is None:
        return None
    total, currency = quote.total_cents, quote.currency
    if fare_ids and offer is not None:
        try:
            total = quote_for_selection(offer, fare_ids, SELECTED_RATIONALE).total_cents
        except InvalidSelection:
            pass
    payer = booking.payer_for(values)  # type: ignore[arg-type]
    card = limit = unavailable = None
    if "wallet" in models:
        card = _wallet_card(payer)
        limit = vault.store().mandate(payer)
        unavailable = vault.wallet_unavailable(payer, total, currency)
    can_wallet = "wallet" in models and unavailable is None
    return PaymentOptions(
        models=list(models),
        default="wallet" if can_wallet else booking.default_model(models),
        payer=payer,
        card=card,
        mandate_cents=limit,
        wallet_unavailable=unavailable,
    )


def _wallet_payment(run_id: str) -> WalletPayment | None:
    record = vault.run_payment(run_id)
    if record is None or not record["redeemed"]:
        return None
    return WalletPayment(
        brand=record["brand"], last4=record["last4"], mandate_cents=record["mandate_cents"]
    )


def _is_terminal(status: str) -> bool:
    return status not in ("running", "awaiting_approval", "awaiting_payment")


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def _run_events(mode: str, chunk: Any, last_status: list[str | None]) -> list[dict]:
    """Translate one LangGraph stream chunk into public run events (no state values)."""
    if mode == "custom":
        if isinstance(chunk, dict) and chunk.get("type") in TOOL_EVENT_TYPES:
            return [{k: v for k, v in chunk.items() if k in TOOL_EVENT_KEYS}]
        return []
    if mode != "tasks" or not isinstance(chunk, dict):
        return []
    node = chunk.get("name")
    if "result" not in chunk:  # task start
        return [{"type": "node_started", "node": node}]
    events: list[dict] = [
        {"type": "node_finished", "node": node, "interrupted": bool(chunk.get("interrupts"))}
    ]
    result = chunk.get("result")
    status = result.get("status") if isinstance(result, dict) else None
    if isinstance(status, str) and status != last_status[0]:
        last_status[0] = status
        events.append({"type": "status", "status": status})
    return events


def create_app(
    llm: LLM | None = None,
    tools: GatedToolClient | None = None,
    audit: AuditLog | None = None,
    db_path: Path | str | None = None,
    auth: AuthConfig | None = None,
) -> FastAPI:
    """Build the app. Defaults: LLM from ``backend/config/llm.yaml``, stdio MCP servers, env audit
    path, checkpoints and run history in ``LAB_DB_PATH`` (opened and closed by the lifespan), and
    authentication from the environment (raises ``AuthConfigError`` when it is not configured)."""
    auth_cfg = auth if auth is not None else AuthConfig.from_env()
    if not auth_cfg.enabled:
        _LOG.warning(
            "API AUTHENTICATION IS DISABLED (API_AUTH_DISABLED=1): anyone who can reach this "
            "API can start runs and approve payments. Local development and tests only."
        )
    login_limiter = RateLimiter(auth_cfg.login_per_min)
    runs_limiter = RateLimiter(auth_cfg.runs_per_min)
    origins = _allowed_origins()
    if audit is None:
        audit = AuditLog(paths.default_audit_log_path())
    if tools is None:
        tools = GatedToolClient.stdio(ToolPolicy.from_yaml(), audit)
    graph_llm = llm or _default_llm()
    store_path = Path(db_path) if db_path is not None else lab_db_path()
    # Set by the lifespan: the graph over the persistent checkpointer, and the run history.
    graph: Any = None
    runs: RunStore | None = None
    locks: dict[str, asyncio.Lock] = {}
    # Strong references to running stream tasks (asyncio keeps only weak ones).
    running: set[asyncio.Task] = set()
    # One webhook delivery at a time: a redelivery racing its original applies effects once.
    webhook_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal graph, runs
        # Only the default LLM is probed: an injected one (tests) is the caller's business.
        if llm is None:
            await _probe_llm()
        async with open_checkpointer(store_path) as saver, RunStore.open(store_path) as store:
            graph, runs = build_graph(graph_llm, tools, checkpointer=saver), store
            _LOG.info("Persistence: %s", store_path)
            try:
                yield
            finally:
                # Let in-flight streamed runs finish their writes before the DB closes.
                if running:
                    await asyncio.gather(*running, return_exceptions=True)
                graph, runs = None, None

    # With authentication on, the schema and docs pages are not served either (every endpoint
    # except the public ones needs a session); the schema is in the repository.
    docs: dict[str, Any] = {}
    if auth_cfg.enabled:
        docs = {"openapi_url": None, "docs_url": None, "redoc_url": None}
    app = FastAPI(title="regulated-agent-lab", version="0.1.0", lifespan=lifespan, **docs)
    app.add_middleware(
        CORSMiddleware,
        # Exact origins only, never "*": the session cookie is sent with credentials.
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        expose_headers=["Retry-After"],
    )
    # Added last, so it wraps everything (CORS preflights and errors get the headers too).
    app.add_middleware(SecurityHeadersMiddleware)

    def _check_origin(request: Request) -> None:
        """CSRF defence in depth: a state-changing request must come from an allowed origin."""
        if request.method not in SAFE_METHODS and request.headers.get("origin") not in origins:
            raise HTTPException(status_code=403, detail="origin not allowed")

    async def require_user(request: Request) -> User | None:
        """The signed-in user; None when authentication is disabled. 401 without a session."""
        if not auth_cfg.enabled:
            return None
        _check_origin(request)
        user = auth_cfg.user_for(request.cookies.get(COOKIE_NAME))
        if user is None:
            raise HTTPException(status_code=401, detail="not signed in")
        return user

    # A default, not an Annotated alias: this module uses postponed annotations, and FastAPI
    # cannot resolve a name local to this function from a string annotation.
    current_user = Depends(require_user)
    # Every route on this router needs a session (and the Origin check for POST).
    protected = APIRouter(dependencies=[Depends(require_user)])

    def _limit(user: User | None) -> None:
        if user is None:
            return
        wait = runs_limiter.take(user.name)
        if wait is not None:
            raise HTTPException(
                status_code=429, detail="too many requests", headers=retry_after_header(wait)
            )

    def _may_request(user: User | None) -> None:
        _limit(user)
        if user is not None and not user.can_request:
            raise HTTPException(status_code=403, detail="requester role required")

    async def _decision_for(
        run_id: str, body: ApprovalDecision, user: User | None
    ) -> ApprovalDecision:
        """The decision as the server records it: the signed-in user is the approver, and an
        approval that names no payment model gets the proposed one."""
        if user is not None:
            body = await _approver_checks(run_id, body, user)
        if body.approved and body.payment_model is None:
            snapshot = await _graph().aget_state(_config(run_id))
            options = await asyncio.to_thread(payment_options, snapshot.values or {}, body.fare_ids)
            if options is not None and options.default is not None:
                body = body.model_copy(update={"payment_model": options.default})
        return body

    async def _approver_checks(run_id: str, body: ApprovalDecision, user: User) -> ApprovalDecision:
        _limit(user)
        if not user.can_approve:
            raise HTTPException(status_code=403, detail="approver role required")
        if auth_cfg.require_separate_approver:
            snapshot = await _graph().aget_state(_config(run_id))
            requester = (snapshot.values or {}).get("requester")
            if requester is not None and requester == user.name:
                audit.append(
                    run_id,
                    "human",
                    "approval_denied",
                    {"approver": user.name, "reason": "requester cannot approve own run"},
                )
                raise HTTPException(
                    status_code=403, detail="segregation of duties: a second person must approve"
                )
        return body.model_copy(update={"approver": user.name})

    def _me(user: User | None) -> Me:
        if user is None:
            return Me(
                name="local-dev",
                role="both",
                auth_enabled=False,
                require_separate_approver=auth_cfg.require_separate_approver,
            )
        return Me(
            name=user.name,
            role=user.role,
            auth_enabled=True,
            require_separate_approver=auth_cfg.require_separate_approver,
        )

    def _config(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def _graph() -> Any:
        if graph is None:
            raise HTTPException(status_code=503, detail="persistence not open")
        return graph

    def _runs() -> RunStore:
        if runs is None:
            raise HTTPException(status_code=503, detail="persistence not open")
        return runs

    def _lock(run_id: str) -> asyncio.Lock:
        # Created on demand: a run paused before a restart has no lock in this process yet.
        return locks.setdefault(run_id, asyncio.Lock())

    async def _record(view: RunView) -> RunView:
        """Mirror a run's current status, total and payment into the run history."""
        quote = view.quote or {}
        initial = None
        if view.payment is not None:
            # Simulated captures get none; a processor capture starts pending (or with what
            # its events, which may arrive before the graph finishes, already say).
            initial = await asyncio.to_thread(processor_events.initial_settlement, view.run_id)
        await _runs().update(
            view.run_id,
            status=view.status,
            total_cents=quote.get("total_cents"),
            currency=quote.get("currency"),
            payment_id=view.payment.payment_id if view.payment is not None else None,
            flags=view.flags,
            initial_settlement=initial,
        )
        summary = await _runs().get(view.run_id)
        view.settlement = summary.settlement if summary is not None else None
        return view

    async def _record_after(run_id: str) -> RunView | None:
        """Best effort after a failed graph call: record whatever state the run reached."""
        try:
            return await _record(await _view(run_id))
        except Exception:  # never mask the original error
            return None

    async def _view(run_id: str) -> RunView:
        snapshot = await _graph().aget_state(_config(run_id))
        values: dict[str, Any] = snapshot.values or {}
        if not values:
            raise HTTPException(status_code=404, detail="unknown run")
        quote = values.get("quote")
        interrupts = getattr(snapshot, "interrupts", ()) or ()
        pending = interrupts[0].value if interrupts else None
        checkout = None
        # The payment pause carries the processor's page; only the approval pause is "interrupt",
        # so an approval body can never resume (and answer) the payment pause.
        if isinstance(pending, dict) and "checkout_url" in pending:
            checkout, pending = Checkout.model_validate(pending), None
        summary = await runs.get(run_id) if runs is not None else None
        model = values.get("payment_model")
        options = None
        if pending is not None and values.get("status") == "awaiting_approval":
            options = await asyncio.to_thread(payment_options, values)
        wallet_paid = None
        if model == "wallet" and values.get("payment") is not None:
            wallet_paid = await asyncio.to_thread(_wallet_payment, run_id)
        return RunView(
            run_id=run_id,
            status=values["status"],
            quote=quote.model_dump(mode="json") if quote is not None else None,
            interrupt=pending,
            trajectory=list(values.get("trajectory", [])),
            payment=values.get("payment"),
            flags=list(values.get("flags", [])),
            settlement=summary.settlement if summary is not None else None,
            checkout=checkout,
            payment_model=model,
            payment_fallback=values.get("payment_fallback"),
            payment_options=options,
            wallet_payment=wallet_paid,
        )

    async def _stream_graph(graph_input: Any, run_id: str, queue: asyncio.Queue) -> None:
        last_status: list[str | None] = [None]
        async for mode, chunk in _graph().astream(
            graph_input, _config(run_id), stream_mode=STREAM_MODES
        ):
            for event in _run_events(mode, chunk, last_status):
                await queue.put(event)
        view = await _record(await _view(run_id))
        # Always the last status before run_view, even when unchanged since the last node.
        await queue.put({"type": "status", "status": view.status})
        await queue.put({"type": "run_view", **view.model_dump(mode="json")})

    def _start(work: Any, queue: asyncio.Queue) -> None:
        async def runner() -> None:
            try:
                await work()
            except Exception as exc:  # only the type: messages can carry input values
                await queue.put({"type": "error", "error_type": type(exc).__name__})
            finally:
                await queue.put(None)

        task = asyncio.create_task(runner())
        running.add(task)
        task.add_done_callback(running.discard)

    async def _drain(queue: asyncio.Queue) -> AsyncIterator[str]:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEP_ALIVE_S)
            except TimeoutError:
                yield SSE_KEEP_ALIVE
                continue
            if event is None:
                return
            yield _sse(event)

    @protected.post("/runs/stream")
    async def create_run_stream(
        body: CreateRun, user: User | None = current_user
    ) -> StreamingResponse:
        _may_request(user)
        requester = user.name if user is not None else None
        run_id = uuid.uuid4().hex
        lock = _lock(run_id)
        store = _runs()
        queue: asyncio.Queue = asyncio.Queue()

        async def work() -> None:
            async with lock:
                await store.start(run_id, body.message, requester)
                await queue.put({"type": "run_started", "run_id": run_id})
                try:
                    state = initial_state(run_id, body.message, requester)
                    await _stream_graph(state, run_id, queue)
                except Exception:
                    await _record_after(run_id)
                    raise

        _start(work, queue)
        return StreamingResponse(_drain(queue), media_type="text/event-stream", headers=SSE_HEADERS)

    @protected.post("/runs/{run_id}/approval/stream")
    async def approve_stream(
        run_id: str, body: ApprovalDecision, user: User | None = current_user
    ) -> StreamingResponse:
        current = await _view(run_id)  # 404 for an unknown run (also after a restart)
        body = await _decision_for(run_id, body, user)
        lock = _lock(run_id)
        # Fast path for a double click: a resume in flight, or nothing left to approve.
        if lock.locked() or current.interrupt is None:
            raise HTTPException(status_code=409, detail="run is not awaiting approval")
        queue: asyncio.Queue = asyncio.Queue()

        async def work() -> None:
            async with lock:
                # Re-checked under the lock: another approval may have won the race.
                if (await _view(run_id)).interrupt is None:
                    await queue.put(
                        {"type": "error", "status_code": 409, "detail": "not awaiting approval"}
                    )
                    return
                await queue.put({"type": "run_started", "run_id": run_id})
                try:
                    await _stream_graph(Command(resume=body.model_dump()), run_id, queue)
                except Exception:
                    await _record_after(run_id)
                    raise

        _start(work, queue)
        return StreamingResponse(_drain(queue), media_type="text/event-stream", headers=SSE_HEADERS)

    @protected.post("/runs", response_model=RunView)
    async def create_run(body: CreateRun, user: User | None = current_user) -> RunView:
        _may_request(user)
        requester = user.name if user is not None else None
        run_id = uuid.uuid4().hex
        async with _lock(run_id):
            await _runs().start(run_id, body.message, requester)
            try:
                state = initial_state(run_id, body.message, requester)
                await _graph().ainvoke(state, _config(run_id))
            except Exception:
                await _record_after(run_id)
                raise
            return await _record(await _view(run_id))

    @protected.get("/runs", response_model=list[RunSummary])
    async def list_runs(limit: int = Query(default=20, ge=1, le=100)) -> list[RunSummary]:
        """Recent runs, newest first. The request is stored redacted, never raw."""
        return await _runs().recent(limit)

    @protected.get("/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: str) -> RunView:
        return await _view(run_id)

    @protected.post("/runs/{run_id}/approval", response_model=RunView)
    async def approve(
        run_id: str, body: ApprovalDecision, user: User | None = current_user
    ) -> RunView:
        await _view(run_id)  # 404 for an unknown run (also after a restart)
        body = await _decision_for(run_id, body, user)
        async with _lock(run_id):
            current = await _view(run_id)
            if current.interrupt is None:
                raise HTTPException(status_code=409, detail="run is not awaiting approval")
            try:
                await _graph().ainvoke(Command(resume=body.model_dump()), _config(run_id))
            except Exception:
                await _record_after(run_id)
                raise
            return await _record(await _view(run_id))

    @protected.post("/runs/{run_id}/quote", response_model=Quote)
    async def quote_selection(run_id: str, body: QuoteRequest) -> Quote:
        """The exact quote (total and hash) a selection would be approved for. No side effects."""
        snapshot = await _graph().aget_state(_config(run_id))
        values: dict[str, Any] = snapshot.values or {}
        if not values:
            raise HTTPException(status_code=404, detail="unknown run")
        offer, recommended = values.get("offer"), values.get("quote")
        paused = bool(getattr(snapshot, "interrupts", ())) and values.get("status") == (
            "awaiting_approval"
        )
        if not paused or offer is None or recommended is None:
            raise HTTPException(status_code=409, detail="run is not awaiting approval")
        try:
            selected = quote_for_selection(offer, body.fare_ids, SELECTED_RATIONALE)
        except InvalidSelection as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if selected.quote_hash == recommended.quote_hash:
            return recommended
        return selected

    @app.post("/webhooks/stripe")
    async def processor_webhook(request: Request) -> JSONResponse:
        """Signed processor events. 503 without a secret, 400 for a bad signature or body;
        both store nothing. 200 for every verified event, also a redelivery (no second effect)
        and a live-mode event (recorded, never applied), so the processor stops retrying."""
        if processor_events.webhook_secret() is None:
            raise HTTPException(status_code=503, detail="webhooks not configured")
        raw = await request.body()
        signature = request.headers.get(processor_events.SIGNATURE_HEADER)
        async with webhook_lock:
            try:
                ingested = await asyncio.to_thread(processor_events.ingest, raw, signature)
            except processor_events.WebhookNotConfigured as exc:
                raise HTTPException(status_code=503, detail="webhooks not configured") from exc
            except processor_events.SignatureError as exc:
                raise HTTPException(status_code=400, detail="invalid signature") from exc
            except processor_events.MalformedEvent as exc:
                raise HTTPException(status_code=400, detail="malformed event") from exc
            if ingested.disposition == "ignored" or not ingested.needs_forward:
                duplicate = ingested.disposition != "ignored"
                return JSONResponse({"received": True, "duplicate": duplicate})
            session = ingested.event.checkout_session or {}
            live = ingested.disposition == processor_events.LIVEMODE_REJECTED
            if ingested.is_checkout and not live and session.get("mode") == "setup":
                await _wallet_setup_event(ingested)
                await asyncio.to_thread(
                    processor_events.event_store().mark_forwarded, ingested.event.event_id
                )
                return JSONResponse({"received": True, "duplicate": False, "disposition": "wallet"})
            if ingested.is_checkout and not live:
                await _checkout_event(ingested)
                await asyncio.to_thread(
                    processor_events.event_store().mark_forwarded, ingested.event.event_id
                )
                return JSONResponse(
                    {"received": True, "duplicate": False, "disposition": ingested.disposition}
                )
            if ingested.run_id is not None and ingested.settlement is not None:
                await _runs().set_settlement(ingested.run_id, ingested.settlement)
            run_id, event, data = _event_audit(ingested)
            # If this write fails the event stays unforwarded and the 500 makes the processor
            # redeliver it: effects are applied at least once, never silently dropped.
            audit.append(run_id, "processor", event, data)
            await asyncio.to_thread(
                processor_events.event_store().mark_forwarded, ingested.event.event_id
            )
        return JSONResponse(
            {"received": True, "duplicate": False, "disposition": ingested.disposition}
        )

    async def _checkout_event(ingested: processor_events.Ingested) -> None:
        """A verified checkout.session.* event: apply it to its run, or audit it unmatched."""
        session = ingested.event.checkout_session or {}
        if ingested.disposition == processor_events.MATCHED and ingested.run_id is not None:
            try:
                await _settle_checkout(ingested.run_id, "webhook", session=session)
                return
            except HTTPException as exc:
                # The ledger knows the session but no run state does (e.g. lab.db was reset):
                # record it like an unmatched event, so the processor stops redelivering.
                if exc.status_code != 404:
                    raise
        audit.append(
            PROCESSOR_AUDIT_RUN,
            "processor",
            "checkout_event_unmatched",
            {
                "event_id": ingested.event.event_id,
                "event_type": ingested.event.type,
                "session_id": session.get("id"),
                "disposition": ingested.disposition,
            },
        )

    async def _wallet_setup_event(ingested: processor_events.Ingested) -> None:
        """A verified ``checkout.session.completed`` in ``mode=setup``: the wallet saves the card.

        Only the session id is taken from the event; the wallet reads the session back from the
        processor and keeps brand, last four digits, expiry and the processor ids.
        """
        session_id = (ingested.event.checkout_session or {}).get("id")
        try:
            outcome = await asyncio.to_thread(
                vault.complete_setup_session, session_id, transport=vault.PROCESSOR_TRANSPORT
            )
        except processor.ProcessorNotConfigured as exc:
            raise HTTPException(status_code=503, detail="payment processor not configured") from exc
        except (processor.ProcessorUnavailable, wallet_processor.SetupNotFound) as exc:
            raise HTTPException(status_code=502, detail="payment processor unavailable") from exc
        if outcome.changed and outcome.user is not None:
            audit.append(
                WALLET_AUDIT_RUN,
                "wallet",
                "wallet_card_saved",
                {"user": outcome.user, "source": "webhook", "session_id": session_id},
            )
        elif outcome.state in ("not_found", "refused"):
            audit.append(
                PROCESSOR_AUDIT_RUN,
                "processor",
                "wallet_setup_unmatched",
                {
                    "event_id": ingested.event.event_id,
                    "session_id": session_id,
                    "reason": outcome.reason or outcome.state,
                },
            )

    def _audit_checkout(run_id: str, outcome: checkouts.CheckoutOutcome, source: str) -> None:
        data = outcome.audit_data(source)
        if outcome.state == checkouts.CANCELED:
            data.setdefault("reason", "canceled by customer")
        if outcome.changed and outcome.state in CHECKOUT_AUDIT:
            audit.append(run_id, "processor", CHECKOUT_AUDIT[outcome.state], data)
        elif outcome.reason == "reference_mismatch":
            audit.append(run_id, "processor", "checkout_refused", data)

    async def _settle_checkout(run_id: str, source: str, session: dict | None = None) -> RunView:
        """Apply the processor's record of the run's checkout and resume the paused run once.

        ``source``: ``webhook`` (``session`` is the verified event's), ``refresh`` or ``cancel``
        (read from, or closed at, the processor). Under the run's lock; the ledger transition is
        conditional, so whichever path comes second finds the work done and changes nothing.
        """
        await _view(run_id)  # 404 for an unknown run
        async with _lock(run_id):
            view = await _view(run_id)
            if view.status != "awaiting_payment" or view.checkout is None:
                if source == "webhook" or _is_terminal(view.status):
                    return view
                raise HTTPException(status_code=409, detail="run is not awaiting payment")
            transport = payments_server.PROCESSOR_TRANSPORT
            try:
                if session is not None:
                    outcome = await asyncio.to_thread(
                        checkouts.apply_session, session, transport=transport
                    )
                elif source == "cancel":
                    outcome = await asyncio.to_thread(checkouts.cancel, run_id, transport=transport)
                else:
                    outcome = await asyncio.to_thread(
                        checkouts.refresh, run_id, transport=transport
                    )
            except processor.ProcessorNotConfigured as exc:
                raise HTTPException(
                    status_code=503, detail="payment processor not configured"
                ) from exc
            except (
                processor.ProcessorUnavailable,
                processor.CheckoutNotFound,
                processor.PaymentDeclined,
            ) as exc:
                raise HTTPException(
                    status_code=502, detail="payment processor unavailable"
                ) from exc
            _audit_checkout(run_id, outcome, source)
            if outcome.state not in checkouts.TERMINAL:
                return view
            try:
                await _graph().ainvoke(Command(resume={"source": source}), _config(run_id))
            except Exception:
                await _record_after(run_id)
                raise
            done = await _view(run_id)
            if done.status == "completed" and outcome.state == checkouts.CAPTURED:
                # A paid session is the processor's own confirmation of this payment.
                await _runs().confirm_settlement(run_id)
            return await _record(done)

    @protected.post("/runs/{run_id}/payment/refresh", response_model=RunView)
    async def refresh_payment(run_id: str, user: User | None = current_user) -> RunView:
        """Ask the processor about the run's checkout now (no webhook needed)."""
        _limit(user)
        return await _settle_checkout(run_id, "refresh")

    @protected.post("/runs/{run_id}/payment/cancel", response_model=RunView)
    async def cancel_payment(run_id: str, user: User | None = current_user) -> RunView:
        """Close the run's checkout at the processor; the run fails (unless it was paid)."""
        _limit(user)
        return await _settle_checkout(run_id, "cancel")

    @protected.post("/reconcile", response_model=ReconcileResult)
    async def run_reconciliation() -> ReconcileResult:
        """Compare every test-mode capture with the processor's record (read-only there)."""
        try:
            report = await asyncio.to_thread(
                reconciliation.reconcile, payments_server.PROCESSOR_TRANSPORT
            )
        except processor.ProcessorNotConfigured as exc:
            raise HTTPException(status_code=503, detail="payment processor not configured") from exc
        updates: list[SettlementUpdate] = []
        for item in report.payments:
            target = item.processor_settlement
            if target is None or target == "pending":
                continue
            summary = await _runs().get(item.run_id)
            if summary is None or summary.settlement == target:
                continue
            previous = await _runs().set_settlement(item.run_id, target)  # type: ignore[arg-type]
            audit.append(
                item.run_id,
                "processor",
                "settlement_reconciled",
                {
                    "payment_id": item.payment_id,
                    "previous": previous,
                    "settlement": target,
                    "source": "reconciliation",
                    "discrepancies": item.discrepancies,
                },
            )
            updates.append(
                SettlementUpdate(
                    run_id=item.run_id,
                    payment_id=item.payment_id,
                    previous=previous,  # type: ignore[arg-type]
                    settlement=target,  # type: ignore[arg-type]
                )
            )
        audit.append(
            RECONCILE_AUDIT_RUN,
            "reconciliation",
            "reconciliation_run",
            {**report.audit_data(), "settlement_updates": len(updates)},
        )
        return ReconcileResult(report=report, settlement_updates=updates)

    # --- wallet (the signed-in user's saved card and spending limit) ---------------------------

    def _wallet_user(user: User | None) -> str:
        return user.name if user is not None else booking.LOCAL_PAYER

    def _wallet_enabled() -> bool:
        try:
            return "wallet" in booking.payment_models()
        except ValueError:
            return False

    def _wallet_view(name: str, setup_state: str | None = None) -> WalletView:
        return WalletView(
            user=name,
            enabled=_wallet_enabled(),
            card=_wallet_card(name),
            mandate_cents=vault.store().mandate(name),
            setup_state=setup_state,
        )

    def _processor_errors(exc: Exception) -> HTTPException:
        if isinstance(exc, processor.ProcessorNotConfigured):
            return HTTPException(status_code=503, detail="payment processor not configured")
        return HTTPException(status_code=502, detail="payment processor unavailable")

    _PROCESSOR_FAILURES = (
        processor.ProcessorNotConfigured,
        processor.ProcessorUnavailable,
        wallet_processor.SetupNotFound,
    )

    @protected.get("/wallet", response_model=WalletView)
    async def read_wallet(user: User | None = current_user) -> WalletView:
        return await asyncio.to_thread(_wallet_view, _wallet_user(user))

    @protected.post("/wallet/setup", response_model=WalletSetup)
    async def wallet_setup(user: User | None = current_user) -> WalletSetup:
        """Open the processor's hosted card form (mode=setup); the browser goes there."""
        _limit(user)
        if not _wallet_enabled():
            raise HTTPException(status_code=409, detail="wallet payments are not enabled")
        name = _wallet_user(user)
        try:
            started = await asyncio.to_thread(
                vault.start_setup, name, transport=vault.PROCESSOR_TRANSPORT
            )
        except _PROCESSOR_FAILURES as exc:
            raise _processor_errors(exc) from exc
        except ValueError as exc:  # PUBLIC_APP_URL
            raise HTTPException(status_code=500, detail="PUBLIC_APP_URL is invalid") from exc
        audit.append(
            WALLET_AUDIT_RUN,
            "wallet",
            "wallet_setup_started",
            {"user": name, "session_id": started["session_id"]},
        )
        return WalletSetup(setup_url=started["setup_url"])

    @protected.post("/wallet/setup/complete", response_model=WalletView)
    async def wallet_setup_complete(user: User | None = current_user) -> WalletView:
        """Back from the processor (``?wallet=saved``): the wallet reads the setup back."""
        _limit(user)
        name = _wallet_user(user)
        try:
            outcome = await asyncio.to_thread(
                vault.complete_setup, name, transport=vault.PROCESSOR_TRANSPORT
            )
        except _PROCESSOR_FAILURES as exc:
            raise _processor_errors(exc) from exc
        if outcome.changed:
            audit.append(
                WALLET_AUDIT_RUN,
                "wallet",
                "wallet_card_saved",
                {"user": name, "source": "return", "session_id": outcome.session_id},
            )
        return await asyncio.to_thread(_wallet_view, name, outcome.state)

    @protected.post("/wallet/card/remove", response_model=WalletView)
    async def wallet_remove_card(user: User | None = current_user) -> WalletView:
        _limit(user)
        name = _wallet_user(user)
        removed = await asyncio.to_thread(
            vault.remove_card, name, transport=vault.PROCESSOR_TRANSPORT
        )
        if removed:
            audit.append(WALLET_AUDIT_RUN, "wallet", "wallet_card_removed", {"user": name})
        return await asyncio.to_thread(_wallet_view, name)

    @protected.post("/wallet/mandate", response_model=WalletView)
    async def wallet_mandate(body: MandateUpdate, user: User | None = current_user) -> WalletView:
        """Set the spending limit per booking (integer cents); audited when it changes."""
        _limit(user)
        name = _wallet_user(user)
        previous = await asyncio.to_thread(vault.set_mandate, name, body.limit_cents)
        if previous != body.limit_cents:
            audit.append(
                WALLET_AUDIT_RUN,
                "wallet",
                "wallet_mandate_changed",
                {
                    "user": name,
                    "previous_cents": previous,
                    "limit_cents": body.limit_cents,
                    "currency": vault.CURRENCY,
                },
            )
        return await asyncio.to_thread(_wallet_view, name)

    @protected.get("/audit", response_model=list[AuditEntry])
    async def read_audit(run_id: str | None = Query(default=None)) -> list[AuditEntry]:
        return audit.read(run_id)

    @protected.get("/mode", response_model=Mode)
    async def read_mode() -> Mode:
        return current_mode()

    @protected.get("/audit/verify", response_model=VerifyResult)
    async def verify_audit() -> VerifyResult:
        return audit.verify()

    @protected.get("/auth/me", response_model=Me)
    async def me(user: User | None = current_user) -> Me:
        return _me(user)

    # --- public routes ----------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    def _client_ip(request: Request) -> str:
        # Behind a reverse proxy, run uvicorn with --proxy-headers --forwarded-allow-ips so this
        # is the client's address, not the proxy's (else all clients share one login bucket).
        return request.client.host if request.client else "unknown"

    @app.post("/auth/login", response_model=Me)
    async def login(body: Login, request: Request) -> JSONResponse:
        if not auth_cfg.enabled:
            raise HTTPException(status_code=404, detail="authentication is disabled")
        _check_origin(request)
        wait = login_limiter.take(_client_ip(request))
        if wait is not None:
            raise HTTPException(
                status_code=429, detail="too many login attempts", headers=retry_after_header(wait)
            )
        user = await asyncio.to_thread(auth_cfg.authenticate, body.name, body.password)
        if user is None:
            # Only a known name is recorded: free text from an anonymous caller is not.
            known = body.name if body.name in auth_cfg.users else None
            audit.append(AUTH_AUDIT_RUN, "auth", "login_failed", {"user": known})
            raise HTTPException(status_code=401, detail="invalid name or password")
        audit.append(AUTH_AUDIT_RUN, "auth", "login", {"user": user.name, "role": user.role})
        response = JSONResponse(_me(user).model_dump())
        response.set_cookie(
            COOKIE_NAME,
            auth_cfg.issue(user),
            max_age=auth_cfg.session_ttl_s,
            httponly=True,
            samesite="strict",
            secure=auth_cfg.cookie_secure,
            path="/",
        )
        return response

    @app.post("/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        """Ends the session (idempotent): the cookie is cleared and its token revoked here."""
        if auth_cfg.enabled:
            _check_origin(request)
            token = request.cookies.get(COOKIE_NAME)
            user = auth_cfg.user_for(token)
            auth_cfg.revoke(token)
            if user is not None:
                audit.append(AUTH_AUDIT_RUN, "auth", "logout", {"user": user.name})
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(
            COOKIE_NAME,
            httponly=True,
            samesite="strict",
            secure=auth_cfg.cookie_secure,
            path="/",
        )
        return response

    app.include_router(protected)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Lazy ``app`` for ``uvicorn regulated_agent_lab.api.server:app``.

    Importing the module has no side effects.
    """
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


__all__ = [
    "ALLOWED_ORIGINS",
    "EXTRA_ORIGINS_ENV",
    "ApprovalDecision",
    "Checkout",
    "CreateRun",
    "MandateUpdate",
    "Me",
    "Payment",
    "PaymentOptions",
    "QuoteRequest",
    "ReconcileResult",
    "RunSummary",
    "RunView",
    "SettlementUpdate",
    "WalletView",
    "create_app",
    "payment_options",
]
