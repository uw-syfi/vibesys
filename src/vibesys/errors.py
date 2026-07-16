"""Typed user-facing failures shared by CLI and supervision transports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigurationDiagnostic:
    """Presentation-neutral description of an invalid run configuration."""

    code: str
    stage: str
    message: str
    usage: str | None = None
    exit_code: int = 2


class ConfigurationError(Exception):
    """Raised when user input cannot be resolved into a runnable session."""

    def __init__(self, diagnostic: ConfigurationDiagnostic):
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
