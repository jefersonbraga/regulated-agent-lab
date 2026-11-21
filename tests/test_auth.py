"""API authentication with auth ON: demo users, signed session cookies, CSRF origin check,
roles, segregation of duties and rate limits. (The other API tests run with it disabled.)"""

from contextlib import asynccontextmanager

import httpx
import pytest
from httpx import ASGITransport

from agents.llm import MockLLM
from agents.tools import GatedToolClient, make_clients
from api import auth
from api.auth import AuthConfig, AuthConfigError, RateLimiter
from api.server import create_app
from controls.audit_log import AuditLog
from controls.policy import ToolPolicy
from mcp_servers.payments_sandbox.server import reset_ledger
from tests.test_api import FIXTURES, MESSAGE

ORIGIN = {"Origin": "http://localhost:5173"}
SECRET = "s" * 40
# Hashing once per module keeps the suite fast (scrypt is slow on purpose).
HASHES = {name: auth.hash_password(f"{name}-pw") for name in ("alice", "rita", "bob")}
USERS = f"alice:{HASHES['alice']},rita:requester:{HASHES['rita']},bob:approver:{HASHES['bob']}"


def _config(**overrides) -> AuthConfig:
    env = {"DEMO_USERS": USERS, "SESSION_SECRET": SECRET, **overrides.pop("env", {})}
    cfg = AuthConfig.from_env(env)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture(autouse=True)
def _clean():
    reset_ledger()
    yield
    reset_ledger()


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@asynccontextmanager
async def serve_auth(audit, tmp_path, cfg: AuthConfig | None = None):
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    app = create_app(
        llm=MockLLM(FIXTURES),
        tools=tools,
        audit=audit,
        db_path=tmp_path / "lab.db",
        auth=cfg or _config(),
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def client(audit, tmp_path):
    async with serve_auth(audit, tmp_path) as c:
        yield c


async def _login(client, name: str, password: str | None = None) -> httpx.Response:
    return await client.post(
        "/auth/login", json={"name": name, "password": password or f"{name}-pw"}, headers=ORIGIN
    )


async def _run(client) -> dict:
    resp = await client.post("/runs", json={"message": MESSAGE}, headers=ORIGIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _decision(run: dict, approver: str = "mallory") -> dict:
    return {"approved": True, "approver": approver, "quote_hash": run["interrupt"]["quote_hash"]}


# --- configuration: fail closed ---------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"DEMO_USERS": USERS},
        {"SESSION_SECRET": SECRET},
        {"DEMO_USERS": USERS, "SESSION_SECRET": "short"},
        {"DEMO_USERS": "alice:plaintext", "SESSION_SECRET": SECRET},
        {"DEMO_USERS": f"alice:admin:{HASHES['alice']}", "SESSION_SECRET": SECRET},
        {"DEMO_USERS": f"a b:{HASHES['alice']}", "SESSION_SECRET": SECRET},
        {"DEMO_USERS": " , ", "SESSION_SECRET": SECRET},
    ],
)
def test_missing_or_bad_configuration_refuses_to_start(env):
    with pytest.raises(AuthConfigError):
        AuthConfig.from_env(env)


def test_the_app_refuses_to_start_without_auth_config(monkeypatch, audit, tmp_path):
    for name in ("API_AUTH_DISABLED", "DEMO_USERS", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    tools = GatedToolClient(make_clients(in_memory=True), ToolPolicy.from_yaml(), audit)
    with pytest.raises(AuthConfigError):
        create_app(llm=MockLLM(FIXTURES), tools=tools, audit=audit, db_path=tmp_path / "x.db")


def test_auth_is_only_off_when_disabled_explicitly():
    assert AuthConfig.from_env({"API_AUTH_DISABLED": "1"}).enabled is False
    assert AuthConfig.from_env({"DEMO_USERS": USERS, "SESSION_SECRET": SECRET}).enabled is True
    with pytest.raises(AuthConfigError):
        AuthConfig.from_env({"API_AUTH_DISABLED": "true"})


def test_password_hashes_and_roles():
    encoded = HASHES["alice"]
    scheme, salt, digest = encoded.split("$")
    assert scheme == "scrypt"
    assert len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
    assert auth.verify_password("alice-pw", encoded)
    assert not auth.verify_password("alice-pw ", encoded)
    users = auth.parse_users(USERS)
    assert {n: u.role for n, (u, _) in users.items()} == {
        "alice": "both",
        "rita": "requester",
        "bob": "approver",
    }
    assert users["rita"][0].can_request and not users["rita"][0].can_approve
    assert users["bob"][0].can_approve and not users["bob"][0].can_request


def test_hash_password_script_prints_a_usable_entry(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "hash_password.py"
    spec = importlib.util.spec_from_file_location("hash_password", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt="": "s3cret-pass")
    assert module.main(["carol", "--role", "approver"]) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "s3cret-pass" not in line
    ((user, encoded),) = auth.parse_users(line).values()
    assert user.name == "carol" and user.role == "approver"
    assert auth.verify_password("s3cret-pass", encoded)


# --- what is public, what is not --------------------------------------------------------------


async def test_everything_but_the_public_routes_needs_a_session(client):
    assert (await client.get("/health")).json() == {"status": "ok"}
    for path in ("/runs", "/runs/x", "/audit", "/audit/verify", "/mode", "/auth/me"):
        assert (await client.get(path)).status_code == 401, path
    for path in ("/runs", "/runs/stream", "/runs/x/approval", "/runs/x/payment/refresh"):
        resp = await client.post(path, json={}, headers=ORIGIN)
        assert resp.status_code == 401, path
    assert (await client.post("/reconcile", headers=ORIGIN)).status_code == 401
    # No schema or docs either while authentication is on.
    assert (await client.get("/openapi.json")).status_code == 404
    assert (await client.get("/docs")).status_code == 404


async def test_webhook_stays_signature_authenticated_not_session(client, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    resp = await client.post("/webhooks/stripe", content=b"{}")
    assert resp.status_code == 503  # not 401: the processor never has a session


# --- login, cookie, logout --------------------------------------------------------------------


async def test_login_sets_a_strict_http_only_cookie(client, audit):
    resp = await _login(client, "alice")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "name": "alice",
        "role": "both",
        "auth_enabled": True,
        "require_separate_approver": False,
    }
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE_NAME}=")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert f"Max-Age={auth.SESSION_TTL_S}" in cookie
    assert "Secure" not in cookie
    assert (await client.get("/auth/me")).json()["name"] == "alice"
    assert (await client.get("/runs")).status_code == 200
    assert [e.event for e in audit.read("auth")] == ["login"]


async def test_cookie_secure_flag(audit, tmp_path):
    cfg = _config(env={"COOKIE_SECURE": "1"})
    async with serve_auth(audit, tmp_path, cfg) as c:
        assert "Secure" in (await _login(c, "alice")).headers["set-cookie"]


async def test_wrong_password_and_unknown_user_are_401(client, audit):
    assert (await _login(client, "alice", "nope")).status_code == 401
    assert (await _login(client, "nobody", "nope")).status_code == 401
    assert "set-cookie" not in (await _login(client, "alice", "nope")).headers
    failed = [e.data["user"] for e in audit.read("auth") if e.event == "login_failed"]
    assert failed == ["alice", None, "alice"]
    assert "nope" not in audit.path.read_text()


async def test_logout_revokes_the_session(client):
    await _login(client, "alice")
    token = client.cookies.get(auth.COOKIE_NAME)
    assert (await client.post("/auth/logout", headers=ORIGIN)).status_code == 200
    assert (await client.get("/auth/me")).status_code == 401
    # Replaying the old cookie does not bring the session back.
    client.cookies.set(auth.COOKIE_NAME, token)
    assert (await client.get("/auth/me")).status_code == 401


async def test_forged_or_expired_sessions_are_refused(audit, tmp_path):
    now = [1_800_000_000.0]
    cfg = _config(clock=lambda: now[0])
    async with serve_auth(audit, tmp_path, cfg) as c:
        await _login(c, "alice")
        token = c.cookies.get(auth.COOKIE_NAME)
        payload, sig = token.split(".")
        c.cookies.set(auth.COOKIE_NAME, f"{payload}.{sig[:-2]}AA")
        assert (await c.get("/auth/me")).status_code == 401
        other = AuthConfig.from_env({"DEMO_USERS": USERS, "SESSION_SECRET": "o" * 40})
        c.cookies.set(auth.COOKIE_NAME, other.issue(auth.User("alice")))
        assert (await c.get("/auth/me")).status_code == 401
        c.cookies.set(auth.COOKIE_NAME, token)
        assert (await c.get("/auth/me")).status_code == 200
        now[0] += auth.SESSION_TTL_S
        assert (await c.get("/auth/me")).status_code == 401


async def test_a_role_change_ends_old_sessions(audit, tmp_path):
    cfg = _config()
    token = cfg.issue(auth.User("rita", "both"))  # rita is listed as requester only
    async with serve_auth(audit, tmp_path, cfg) as c:
        c.cookies.set(auth.COOKIE_NAME, token)
        assert (await c.get("/auth/me")).status_code == 401


# --- CSRF defence in depth: Origin ------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://evil.example"}, {"Origin": "null"}])
async def test_state_changing_requests_need_an_allowed_origin(client, headers):
    await _login(client, "alice")
    resp = await client.post("/runs", json={"message": MESSAGE}, headers=headers)
    assert resp.status_code == 403
    assert (await client.get("/runs")).status_code == 200  # reads need no Origin
    login = await client.post("/auth/login", json={"name": "alice", "password": "alice-pw"})
    assert login.status_code == 403


async def test_cors_allows_credentials_for_allowed_origins_only(client):
    ok = await client.options("/runs", headers={**ORIGIN, "Access-Control-Request-Method": "POST"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert ok.headers["access-control-allow-credentials"] == "true"
    bad = await client.options(
        "/runs",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in bad.headers


# --- who approves -----------------------------------------------------------------------------


async def test_the_approver_is_the_signed_in_user_not_the_body(client, audit):
    await _login(client, "alice")
    run = await _run(client)
    resp = await client.post(
        f"/runs/{run['run_id']}/approval", json=_decision(run, "mallory"), headers=ORIGIN
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"
    (decision,) = [e for e in audit.read(run["run_id"]) if e.event == "approval_decision"]
    assert decision.data["approver"] == "alice"
    charge = [
        e
        for e in audit.read(run["run_id"])
        if e.event == "tool_call" and e.data["tool"] == "payments.charge"
    ]
    assert charge[0].data["args"]["approval_ref"].startswith("alice:")
    listed = (await client.get("/runs")).json()[0]
    assert listed["requester"] == "alice"


async def test_roles_limit_who_requests_and_who_approves(client):
    await _login(client, "bob")  # approver only
    resp = await client.post("/runs", json={"message": MESSAGE}, headers=ORIGIN)
    assert resp.status_code == 403
    await _login(client, "rita")  # requester only
    run = await _run(client)
    resp = await client.post(f"/runs/{run['run_id']}/approval", json=_decision(run), headers=ORIGIN)
    assert resp.status_code == 403
    await _login(client, "bob")
    resp = await client.post(f"/runs/{run['run_id']}/approval", json=_decision(run), headers=ORIGIN)
    assert resp.status_code == 200 and resp.json()["status"] == "completed"


async def test_segregation_of_duties(audit, tmp_path):
    cfg = _config(env={"REQUIRE_SEPARATE_APPROVER": "1"})
    async with serve_auth(audit, tmp_path, cfg) as c:
        await _login(c, "alice")
        assert (await c.get("/auth/me")).json()["require_separate_approver"] is True
        run = await _run(c)
        url = f"/runs/{run['run_id']}/approval"
        own = await c.post(url, json=_decision(run), headers=ORIGIN)
        assert own.status_code == 403
        stream = await c.post(f"{url}/stream", json=_decision(run), headers=ORIGIN)
        assert stream.status_code == 403
        denied = [e for e in audit.read(run["run_id"]) if e.event == "approval_denied"]
        assert len(denied) == 2 and denied[0].data["approver"] == "alice"
        assert (await c.get(f"/runs/{run['run_id']}")).json()["status"] == "awaiting_approval"

        await _login(c, "bob")
        done = await c.post(url, json=_decision(run), headers=ORIGIN)
        assert done.status_code == 200 and done.json()["status"] == "completed"


# --- rate limits ------------------------------------------------------------------------------


async def test_login_is_rate_limited_per_client(client):
    codes = [(await _login(client, "alice", "wrong")).status_code for _ in range(5)]
    assert codes == [401] * 5
    limited = await _login(client, "alice")  # even the right password waits now
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1


async def test_run_creation_is_rate_limited_per_user(audit, tmp_path):
    async with serve_auth(audit, tmp_path, _config(runs_per_min=2)) as c:
        await _login(c, "alice")
        await _run(c)
        await _run(c)
        limited = await c.post("/runs", json={"message": MESSAGE}, headers=ORIGIN)
        assert limited.status_code == 429 and "retry-after" in limited.headers
        streamed = await c.post("/runs/stream", json={"message": MESSAGE}, headers=ORIGIN)
        assert streamed.status_code == 429


def test_token_bucket_refills():
    now = [0.0]
    limiter = RateLimiter(2, per_s=60, clock=lambda: now[0])
    assert limiter.take("k") is None and limiter.take("k") is None
    wait = limiter.take("k")
    assert wait == pytest.approx(30.0)
    assert limiter.take("other") is None  # buckets are per key
    now[0] += 30
    assert limiter.take("k") is None
    assert limiter.take("k") is not None
