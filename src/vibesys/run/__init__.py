"""Run-lifecycle components extracted from ``_RunContext``.

These are experiment-lifecycle concerns (per-run paths, git snapshot
tracking) rather than reusable standalone libraries, so they live under
``src/vibesys/run/`` instead of ``libs/``.
"""

from vibesys.events import CoreEvent, CoreEventType
from vibesys.repository import RepositoryVisibility

# Compatibility constructor for existing agent callers. New policies use
# vibesys.run.round_transaction.RoundTransactionCoordinator with a typed slot.
from vibesys.run.agent_round_transaction import (
    AgentRoundTransactionCoordinator as RoundTransactionCoordinator,
)
from vibesys.run.device import DeviceLease
from vibesys.run.event_journal import EventJournal
from vibesys.run.experiment_repo import ExperimentRepository
from vibesys.run.git_tracker import GitTracker
from vibesys.run.integration import LocalRunIntegration, RunResourceHandoff

# Compatibility names for existing loop callers. New code passes namespace
# strings to vibesys.run.state.RunState directly.
from vibesys.run.legacy_namespaces import RunStateNamespace
from vibesys.run.legacy_state import LegacyRunState as RunState
from vibesys.run.logger import RunLogger
from vibesys.run.paths import RunCommands, RunPaths
from vibesys.run.project import (
    ProjectProvisioningError,
    ProjectProvisioningSpec,
    provision_project,
)
from vibesys.run.protocol import LoopContext
from vibesys.run.round_transaction import (
    CompletedRound,
    RoundRecoveryOutcome,
    RoundTransaction,
    RoundTransactionError,
)
from vibesys.run.run_control import RunControlChannel, RunStopped, splice_steering
from vibesys.run.workspace import (
    EXCLUDED_WORKSPACE_DIRS,
    CopySpec,
    InputProjectSpec,
    Workspace,
    WorkspaceStep,
)

__all__ = [
    "EXCLUDED_WORKSPACE_DIRS",
    "CompletedRound",
    "CopySpec",
    "CoreEvent",
    "CoreEventType",
    "DeviceLease",
    "EventJournal",
    "ExperimentRepository",
    "GitTracker",
    "InputProjectSpec",
    "LocalRunIntegration",
    "LoopContext",
    "ProjectProvisioningError",
    "ProjectProvisioningSpec",
    "RepositoryVisibility",
    "RoundRecoveryOutcome",
    "RoundTransaction",
    "RoundTransactionCoordinator",
    "RoundTransactionError",
    "RunCommands",
    "RunControlChannel",
    "RunLogger",
    "RunPaths",
    "RunResourceHandoff",
    "RunState",
    "RunStateNamespace",
    "RunStopped",
    "Workspace",
    "WorkspaceStep",
    "provision_project",
    "splice_steering",
]
