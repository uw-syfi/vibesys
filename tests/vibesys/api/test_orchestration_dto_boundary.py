"""Internal DTOs remain identical through their public compatibility facades."""

from __future__ import annotations

import subprocess
import sys

from vibesys.api import (
    OrchestrationRunRequest,
    ResumeRef,
    RunRequestLike,
    RunResult,
    RunStatus,
    RunView,
)
from vibesys.api import contracts as api_contracts
from vibesys.api import run_request as api_request
from vibesys.orchestration.environment import AgentEnvironment
from vibesys.orchestration.request import (
    OrchestrationRunRequest as InternalRequest,
)
from vibesys.orchestration.request import (
    ResumeRef as InternalResumeRef,
)
from vibesys.orchestration.request import (
    RunRequestLike as InternalRequestLike,
)
from vibesys.orchestration.view import RunResult as InternalResult
from vibesys.orchestration.view import RunStatus as InternalStatus
from vibesys.orchestration.view import RunView as InternalView


def test_public_dtos_reexport_internal_classes() -> None:
    assert OrchestrationRunRequest is api_request.OrchestrationRunRequest is InternalRequest
    assert ResumeRef is api_request.ResumeRef is InternalResumeRef
    assert RunRequestLike is api_request.RunRequestLike is InternalRequestLike
    assert RunResult is api_contracts.RunResult is InternalResult
    assert RunStatus is api_contracts.RunStatus is InternalStatus
    assert RunView is api_contracts.RunView is InternalView
    assert api_contracts.AgentEnvironment is AgentEnvironment


def test_internal_dtos_do_not_import_public_api() -> None:
    script = (
        "import sys; "
        "import vibesys.orchestration.request, vibesys.orchestration.view, "
        "vibesys.orchestration.environment; "
        "assert 'vibesys.api' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", script], check=True)  # noqa: S603
