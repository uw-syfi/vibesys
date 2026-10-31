"""Rewinding a killed turn leaves a workspace its role may not write untouched, and restores the rest.

A run resumed after a kill clears the turn the kill left open. The planner and the judge only
read the project, and a resumed run's own project refuses a restore, so a rewind that restored
anyway left the resumed run waiting on a clear that could never succeed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import vs_core.api as core
from vs_project.api import Project
from vs_runtime.api import RuntimeContractError, WorkspaceAccess
from vs_runtime.api.core import AccessGrant, AccessKey, AccessSettlement, ReceiptStore
from vs_runtime.api.testing import FakeWorkspace
from vs_sim.api.testing import SimThreads, VirtualClock, run_virtual

if TYPE_CHECKING:
    from pathlib import Path

_SCOPE = core.Scope(owner=core.RunId(root="run"), generation=0)
_KEY = AccessKey(binding="binding", invocation="inv/1")


class ProtectedWorkspace(FakeWorkspace):
    """A workspace that refuses every restore, as a resumed run's own project does."""

    async def restore_for_agent(self, revision: str, *, preserve_paths: tuple[str, ...]) -> None:
        del revision, preserve_paths
        message = "project content retained; resumed root work requires project-edit protection"
        raise RuntimeContractError(message)


def settlement(root: Path, workspace: ProtectedWorkspace) -> AccessSettlement:
    (root / "project").mkdir(parents=True, exist_ok=True)
    namespace = Project.open(root / "project").state.state_store_namespace("run")

    async def lookup(ref: core.WorkspaceRef | core.Scope) -> ProtectedWorkspace:
        del ref
        return workspace

    return AccessSettlement(ReceiptStore(namespace, threads=SimThreads()), lookup)


async def rewound(root: Path, access: WorkspaceAccess) -> AccessSettlement:
    workspace = ProtectedWorkspace(path=root / "project")
    access_settlement = settlement(root, workspace)
    grant = AccessGrant(role_id="planner", access=access)
    await access_settlement.begin(_KEY, _SCOPE, workspace, grant)
    access_settlement.fence(_KEY)
    await access_settlement.rewind(_KEY)
    return access_settlement


def test_a_turn_its_role_may_not_write_is_rewound_without_a_restore(tmp_path: Path) -> None:
    done = run_virtual(VirtualClock(), rewound(tmp_path, WorkspaceAccess.READ_ONLY))

    receipt = done.receipt(_KEY)
    assert receipt is not None
    assert not receipt.settled


@pytest.mark.parametrize("access", [WorkspaceAccess.LIMITED, WorkspaceAccess.READ_WRITE])
def test_a_turn_its_role_may_write_is_restored_to_its_baseline_or_not_rewound(
    tmp_path: Path, access: WorkspaceAccess
) -> None:
    with pytest.raises(RuntimeContractError, match="project-edit protection"):
        run_virtual(VirtualClock(), rewound(tmp_path, access))
