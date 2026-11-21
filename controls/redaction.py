"""PII redaction for anything that leaves the process (audit log, API responses)."""

from __future__ import annotations

import re
from typing import Any

PII_KEYS = frozenset(
    {
        "name",
        "full_name",
        "first_name",
        "last_name",
        "email",
        "document",
        "document_number",
        "passport",
        "phone",
    }
)

# Bearer credentials that pass through tool calls (the wallet's single-use payment token): never
# written to the audit log, even though they are useless after one use or 15 minutes.
SECRET_KEYS = frozenset({"payment_token"})

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_AHV_RE = re.compile(r"\b756(?:\.\d{4}\.\d{4}\.\d{2}|\d{10})\b")
_PASSPORT_RE = re.compile(r"\b[A-Z]{1,2}\d{6,8}\b")


def _redact_text(text: str) -> str:
    text = _EMAIL_RE.sub("[REDACTED:email]", text)
    text = _AHV_RE.sub("[REDACTED:document]", text)
    text = _PASSPORT_RE.sub("[REDACTED:document]", text)
    return text


def redact(value: Any) -> Any:
    """Return a redacted copy of ``value``; the input is never mutated."""
    if isinstance(value, dict):
        return {
            k: (
                f"[REDACTED:{k}]"
                if isinstance(k, str) and (k in PII_KEYS or k in SECRET_KEYS)
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(redact(v) for v in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value
