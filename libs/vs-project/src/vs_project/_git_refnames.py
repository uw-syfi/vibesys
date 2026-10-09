"""Git's reference-name rules (``git check-ref-format``), as pure functions."""

from __future__ import annotations

_FORBIDDEN = frozenset(" ~^:?*[\\\x7f")
_FIRST_PRINTABLE = 0x20


def is_valid_ref_name(name: str) -> bool:
    """Whether ``name`` is a legal full reference name with at least two components."""
    if "/" not in name or name.startswith("-"):
        return False
    return _legal(name)


def is_valid_branch_name(name: str) -> bool:
    """Whether ``name`` is a legal branch name: ``refs/heads/<name>`` is legal and it is no option."""
    if name.startswith("-") or name == "HEAD":
        return False
    return _legal(f"refs/heads/{name}")


def _legal(name: str) -> bool:
    if not name or name.startswith("/") or name.endswith(("/", ".")):
        return False
    if ".." in name or "@{" in name or "//" in name:
        return False
    if any(ord(char) < _FIRST_PRINTABLE or char in _FORBIDDEN for char in name):
        return False
    return all(
        component and not component.startswith(".") and not component.endswith(".lock")
        for component in name.split("/")
    )
