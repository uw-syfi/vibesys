"""Low-level configuration-error helpers shared across ``entrypoints.cli`` submodules.

Kept dependency-free of the other ``cli`` submodules so it can be imported by
all of them without introducing import cycles.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, NoReturn

from vibesys.api import (
    ArgumentsRejected,
    ArgumentsUnrecognized,
    ConfigurationCause,
    ConfigurationDiagnostic,
    ConfigurationError,
    configuration_error,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class _RunArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports structured configuration errors."""

    def parse_run_arguments(self, args: Sequence[str]) -> argparse.Namespace:
        """Parse as ``parse_args`` does, but report arguments no option takes as that fact."""
        parsed, extras = self.parse_known_args(args)
        if extras:
            raise configuration_error(
                ArgumentsUnrecognized(tuple(extras)), usage=self.format_usage().strip()
            )
        return parsed

    def error(self, message: str) -> NoReturn:
        raise configuration_error(ArgumentsRejected(message), usage=self.format_usage().strip())


def _configuration_error(
    message: str,
    *,
    code: str = "invalid_configuration",
    stage: str = "semantic_validation",
    exit_code: int = 2,
) -> NoReturn:
    raise ConfigurationError(
        ConfigurationDiagnostic(
            code=code,
            stage=stage,
            message=message,
            exit_code=exit_code,
        )
    )


def _cause_error(cause: ConfigurationCause) -> NoReturn:
    """Raise ``cause`` as the run's configuration error, worded for the command line."""
    raise configuration_error(cause)


def _project_resume_mismatch(fields: list[str]) -> NoReturn:
    _configuration_error(
        "Resuming a run cannot change its recorded configuration fields: "
        + ", ".join(sorted(fields)),
        code="project_resume_configuration_mismatch",
        stage="resume_resolution",
    )
