"""Low-level configuration-error helpers shared across ``entrypoints.cli`` submodules.

Kept dependency-free of the other ``cli`` submodules so it can be imported by
all of them without introducing import cycles.
"""

from __future__ import annotations

import argparse
from typing import NoReturn

from vibesys.api import ConfigurationDiagnostic, ConfigurationError


class _RunArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports structured configuration errors."""

    def error(self, message: str) -> NoReturn:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="invalid_arguments",
                stage="argument_parsing",
                message=message,
                usage=self.format_usage().strip(),
            )
        )


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


def _project_resume_mismatch(fields: list[str]) -> NoReturn:
    _configuration_error(
        "Resuming a run cannot change its recorded configuration fields: "
        + ", ".join(sorted(fields)),
        code="project_resume_configuration_mismatch",
        stage="resume_resolution",
    )
