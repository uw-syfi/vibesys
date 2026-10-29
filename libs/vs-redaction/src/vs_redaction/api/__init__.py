"""Pure sanitization for free-form text crossing operator-facing boundaries."""

from __future__ import annotations

import posixpath
import re
from typing import TYPE_CHECKING

from vs_redaction._patterns import SECRET_NAME
from vs_redaction._stream import DiagnosticRedactor

if TYPE_CHECKING:
    from collections.abc import Iterable

_SECRET_ASSIGNMENT = re.compile(rf"(\b{SECRET_NAME}\b\s*[:=]\s*)([^\s,;]+)")
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


HOST_PATH_PLACEHOLDER = "<host-path>"
_TREE_COMPONENTS = 2


def _host_path_roots(paths: Iterable[str]) -> tuple[str, ...]:
    """The path prefixes a mask must hide.

    Paths under one top-level pair of directories (``/work1/user``) are one operator's tree.
    Where several of them share a longer directory, that directory is the root to hide, so
    the files their tools print that no declared path names (a traceback through the
    interpreter's packages) are hidden too. A path with no neighbour is hidden alone.
    """
    absolute = sorted({posixpath.normpath(p) for p in paths if p.startswith("/")})
    trees: dict[tuple[str, ...], list[str]] = {}
    for path in absolute:
        parts = tuple(part for part in path.split("/") if part)
        trees.setdefault(parts[:_TREE_COMPONENTS], []).append(path)
    roots: list[str] = []
    for key, members in trees.items():
        shared = posixpath.commonpath([posixpath.dirname(m) for m in members])
        if len(members) > 1 and len([part for part in shared.split("/") if part]) >= len(key):
            roots.append(shared)
        else:
            roots.extend(members)
    return tuple(roots)


def mask_host_paths(text: str, paths: Iterable[str]) -> str:
    """Replace the operator's private filesystem paths in ``text`` with ``<host-path>``.

    ``paths`` are the absolute paths the operator configured (interpreters, setup scripts,
    workspace roots); relative entries are ignored. The result contains none of them, and
    masking is idempotent. A path is matched only up to a component boundary, so ``/a/b``
    does not hide the start of ``/a/bc``.
    """
    for root in sorted(_host_path_roots(paths), key=len, reverse=True):
        text = re.sub(re.escape(root) + r"(?![\w.-])", HOST_PATH_PLACEHOLDER, text)
    return text


__all__ = [
    "HOST_PATH_PLACEHOLDER",
    "DiagnosticRedactor",
    "mask_host_paths",
    "redact_diagnostic_tail",
    "redact_diagnostic_text",
]
