"""Pure sanitization for free-form text crossing operator-facing boundaries."""

from __future__ import annotations

import re

_COMPACT_SECRET_NAMES = (
    "accesskey",
    "accesstoken",
    "apikey",
    "authtoken",
    "awsaccesskeyid",
    "awssecretaccesskey",
    "bearertoken",
    "clientkey",
    "clientsecret",
    "databasepassword",
    "dbpassword",
    "githubtoken",
    "idtoken",
    "openaiapikey",
    "privatekey",
    "refreshtoken",
    "secretkey",
    "sessiontoken",
)
_COMPACT_SECRET_PATTERN = "|".join(_COMPACT_SECRET_NAMES)
_SECRET_NAME = (
    r"(?:(?i:(?:[a-z0-9]+[_-])+(?:token|password|secret|key)"
    r"(?:[_-][a-z0-9]+)*|" + _COMPACT_SECRET_PATTERN + r"|password|secret|token)|"
    r"[A-Za-z0-9]*(?:Token|Password|Secret|Key)(?:[A-Z][A-Za-z0-9]*)?)"
)
_SECRET_ASSIGNMENT = re.compile(rf"(\b{_SECRET_NAME}\b\s*[:=]\s*)([^\s,;]+)")
_BEARER_TOKEN = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")


def redact_diagnostic_text(text: str) -> str:
    """Redact common credential assignments before they cross a UI boundary."""
    text = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    return _BEARER_TOKEN.sub(r"\1[REDACTED]", text)


def redact_diagnostic_tail(text: str, *, max_chars: int) -> str:
    """Return a bounded diagnostic tail without truncating before redaction.

    Redacting the complete input first is part of the contract: slicing an
    untrusted diagnostic can remove the beginning of a credential key and
    make the remaining assignment impossible for a later boundary to detect.
    """
    if max_chars < 0:
        message = "max_chars must be non-negative"
        raise ValueError(message)
    if max_chars == 0:
        return ""
    return redact_diagnostic_text(text)[-max_chars:]


__all__ = ["redact_diagnostic_tail", "redact_diagnostic_text"]
