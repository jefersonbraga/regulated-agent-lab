import atexit
import os
import shutil
import tempfile

import pytest

os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("AGENT_TOKEN_SECRET", "test")
os.environ.pop("LLM_MODEL", None)  # a deploy's model override must not reach the mock or tests
# Tests and evals never touch the network: the real timetable and the test-mode card processor
# are opt-in for runs only, so a shell that has them set must not leak into a test session.
for _real in (
    "TRAVEL_SEARCH_SOURCE",
    "PAYMENTS_PROVIDER",
    "STRIPE_TEST_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "TIMETABLE_API_URL",
    "PAYMENTS_FLOW",
    "PUBLIC_APP_URL",
    "DEMO_USERS",
    "SESSION_SECRET",
    "COOKIE_SECURE",
    "REQUIRE_SEPARATE_APPROVER",
):
    os.environ.pop(_real, None)
# The API refuses to start without DEMO_USERS and SESSION_SECRET. Existing API tests run with
# authentication explicitly disabled; tests/integration/test_auth.py builds apps with it on.
os.environ["API_AUTH_DISABLED"] = "1"
# Persistence never touches the real var/: one throwaway directory per test session (stdio
# server subprocesses inherit these). Tests that need their own file pass a tmp_path.
_DB_DIR = tempfile.mkdtemp(prefix="lab-test-db-")
atexit.register(shutil.rmtree, _DB_DIR, ignore_errors=True)
os.environ["LAB_DB_PATH"] = os.path.join(_DB_DIR, "lab.db")
os.environ["PAYMENTS_DB_PATH"] = os.path.join(_DB_DIR, "payments.db")
os.environ["WALLET_DB_PATH"] = os.path.join(_DB_DIR, "wallet.db")


@pytest.fixture(autouse=True)
def _clean_wallet():
    """Every test starts with an empty wallet (no saved card, default mandate, no tokens)."""
    from regulated_agent_lab.services.wallet import vault

    vault.reset_wallet()
    yield
    vault.reset_wallet()
