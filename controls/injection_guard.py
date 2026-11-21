"""Prompt-injection guard for tool output.

Tool output is data, never instructions. Every string in a tool result (values and
dict keys) is scanned for known injection patterns; flagged values are replaced and
entries with flagged keys are dropped in the sanitized copy, so structured fields
(prices, ids) survive untouched.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

REMOVED_MARKER = "[REMOVED:suspected-injection]"

INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all )?(previous|prior) instructions",
        r"you are now",
        r"system prompt",
        r"call (the )?(tool|function)",
        r"payments?\.charge",
        r"transfer .* to (iban|account)",
        r"<\/?(system|instructions?)>",
        r"<\/?\s*untrusted_tool_data",
    )
)


class GuardResult(BaseModel):
    flagged: bool
    findings: list[str]
    sanitized: Any


def _match(text: str) -> str | None:
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return None


def _walk(value: Any, path: str, findings: list[str]) -> Any:
    if isinstance(value, str):
        hit = _match(value)
        if hit is not None:
            findings.append(f"{path or '$'}: {hit}")
            return REMOVED_MARKER
        return value
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            key_hit = _match(k) if isinstance(k, str) else None
            if key_hit is not None:
                # Keys reach prompts too (json.dumps); drop the entry, never echo the key.
                findings.append(f"{path or '$'}{{key}}: {key_hit}")
                continue
            out[k] = _walk(v, f"{path}.{k}" if path else str(k), findings)
        return out
    if isinstance(value, list | tuple):
        items = [_walk(v, f"{path}[{i}]", findings) for i, v in enumerate(value)]
        return type(value)(items) if isinstance(value, tuple) else items
    return value


def inspect_tool_output(tool: str, output: Any) -> GuardResult:
    """Scan all strings in ``output``; return findings and a sanitized deep copy.

    ``tool`` is the "server.tool" name; it is recorded for callers/audit and
    does not change the scan.
    """
    findings: list[str] = []
    sanitized = _walk(output, "", findings)
    return GuardResult(flagged=bool(findings), findings=findings, sanitized=sanitized)


def _escape_angle_brackets(text: str) -> str:
    # JSON-style escapes: a JSON payload stays parseable, and no tag (in particular the
    # closing delimiter) can appear inside the block, so the data cannot end it early.
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def as_untrusted_block(text: str) -> str:
    """Wrap free text from a tool so a prompt treats it as data, not instructions.

    ``<`` and ``>`` in ``text`` are escaped, so the wrapped data cannot close the block.
    """
    return f"<untrusted_tool_data>\n{_escape_angle_brackets(text)}\n</untrusted_tool_data>"


__all__ = [
    "GuardResult",
    "INJECTION_PATTERNS",
    "REMOVED_MARKER",
    "as_untrusted_block",
    "inspect_tool_output",
]
