"""Filesystem locations the code relies on, in one place.

* ``BACKEND_DIR``: the ``backend/`` directory (``/app`` in the API image). With the src layout
  this package lives in ``backend/src/regulated_agent_lab``; the project is installed editable
  (``uv sync``), so the path is derived from this file.
* ``config_dir()``: ``backend/config`` (``llm.yaml``, ``tool_policy.yaml``), overridable with
  ``RAL_CONFIG_DIR``.
* ``VAR_DIR``: ``backend/var``, the default home of the runtime data (audit log, SQLite files).
  Each file has its own variable (``AUDIT_LOG_PATH``, ``LAB_DB_PATH``, ``PAYMENTS_DB_PATH``,
  ``WALLET_DB_PATH``) that overrides it.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = PACKAGE_DIR.parent.parent
CONFIG_DIR_ENV = "RAL_CONFIG_DIR"
VAR_DIR = BACKEND_DIR / "var"
MOCK_FIXTURES = PACKAGE_DIR / "agents" / "fixtures" / "mock_roundtrip.json"


def config_dir() -> Path:
    return Path(os.environ.get(CONFIG_DIR_ENV) or BACKEND_DIR / "config")


def config_file(name: str) -> Path:
    return config_dir() / name


def llm_config_path() -> Path:
    return config_file("llm.yaml")


def tool_policy_path() -> Path:
    return config_file("tool_policy.yaml")


def default_audit_log_path() -> Path:
    return Path(os.environ.get("AUDIT_LOG_PATH", str(VAR_DIR / "audit.jsonl")))


__all__ = [
    "BACKEND_DIR",
    "CONFIG_DIR_ENV",
    "MOCK_FIXTURES",
    "PACKAGE_DIR",
    "VAR_DIR",
    "config_dir",
    "config_file",
    "default_audit_log_path",
    "llm_config_path",
    "tool_policy_path",
]
