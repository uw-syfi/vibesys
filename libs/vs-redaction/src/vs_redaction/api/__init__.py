"""Pure sanitization for free-form text crossing operator-facing boundaries."""

from __future__ import annotations

import re

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:[A-Z][A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|KEY)|"
    r"api[_-]?key|auth[_-]?token|password|secret|token)\b\s*[:=]\s*)"
    r"([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")


def redact_diagnostic_text(text: str) -> str:
    """Redact common credential assignments before they cross a UI boundary."""
    text = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    return _BEARER_TOKEN.sub(r"\1[REDACTED]", text)


__all__ = ["redact_diagnostic_text"]
