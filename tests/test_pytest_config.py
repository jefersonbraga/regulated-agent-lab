"""The test run itself is warning-clean, and `pytest -W error` does not stop at config time."""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE = ["tests/test_policy.py", "tests/test_gated_client.py"]
# -W on the command line wins over the ini filterwarnings, so the one third-party warning
# (fastmcp importing the deprecated authlib.jose) is ignored again explicitly here.
_AUTHLIB = "ignore::authlib.deprecate.AuthlibDeprecationWarning"


@pytest.mark.parametrize(
    "extra",
    [[], ["-W", "error", "-W", _AUTHLIB]],
    ids=["default", "warnings-as-errors"],
)
def test_pytest_run_has_no_warnings(extra):
    # An unset pytest-asyncio option warns on every run, and under -W error it is an
    # INTERNALERROR before any test is collected.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra, *_SAMPLE],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output[-3000:]
    assert "warning" not in output.lower(), output[-3000:]
