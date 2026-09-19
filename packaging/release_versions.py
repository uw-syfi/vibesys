"""Canonical cross-ecosystem release version parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PYTHON_RELEASE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:rc(0|[1-9]\d*))?$")
_NPM_RELEASE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.(0|[1-9]\d*))?$")


class ReleaseVersionSyntaxError(ValueError):
    """Raised when a release version is not canonical for its ecosystem."""

    @classmethod
    def invalid_python(cls, source: str, raw: str) -> ReleaseVersionSyntaxError:
        """Build an error for a noncanonical Python project version."""
        return cls(f"{source} must use canonical PEP 440 X.Y.Z or X.Y.ZrcN spelling: {raw!r}")

    @classmethod
    def invalid_npm(cls, source: str, raw: str) -> ReleaseVersionSyntaxError:
        """Build an error for a noncanonical npm package version."""
        return cls(f"{source} must use canonical npm SemVer X.Y.Z or X.Y.Z-rc.N spelling: {raw!r}")


@dataclass(frozen=True)
class ReleaseIdentity:
    """Ecosystem-independent identity for stable and RC releases."""

    major: int
    minor: int
    patch: int
    rc: int | None


def python_release_identity(raw: str, *, source: str) -> ReleaseIdentity:
    """Parse canonical PEP 440 ``X.Y.Z`` or ``X.Y.ZrcN`` spelling."""
    match = _PYTHON_RELEASE.fullmatch(raw)
    if match is None:
        raise ReleaseVersionSyntaxError.invalid_python(source, raw)
    return _identity(match)


def npm_release_identity(raw: str, *, source: str) -> ReleaseIdentity:
    """Parse canonical npm SemVer ``X.Y.Z`` or ``X.Y.Z-rc.N`` spelling."""
    match = _NPM_RELEASE.fullmatch(raw)
    if match is None:
        raise ReleaseVersionSyntaxError.invalid_npm(source, raw)
    return _identity(match)


def _identity(match: re.Match[str]) -> ReleaseIdentity:
    major, minor, patch, rc = match.groups()
    return ReleaseIdentity(int(major), int(minor), int(patch), None if rc is None else int(rc))
