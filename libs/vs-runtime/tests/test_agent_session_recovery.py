"""Shared session contracts for recovery before committing workspace changes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.runtime_agent_sessions import (
    _open_resume_contract,
    _open_session_contract,
    _resume_transport,
    _WorkspaceResource,
)

from vs_agent.api import Completed
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import AgentRole, RuntimeContractError, WorkspaceAccess, WorkspaceRestoreError
from vs_runtime.api.testing import FakeWorkspace

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api import AgentTurnRequest
    from vs_prompts.api import RenderedPrompt
    from vs_runtime.api import AgentSession


class _RestorationFault:
    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.recovery_allowed = False
        self.attempts: list[str] = []
        self.changes: list[str] = []
        self.commits: list[tuple[str, ...]] = []

    def restore(self, revision: str, preserve_paths: tuple[str, ...]) -> bool:
        self.attempts.append(revision)
        if not self.recovery_allowed:
            if self.failure == "raise":
                raise WorkspaceRestoreError(revision)
            return False
        self.changes[:] = [path for path in self.changes if path in preserve_paths]
        return True

    def commit(self) -> None:
        self.commits.append(tuple(self.changes))
        self.changes.clear()


class _FaultedFakeWorkspace(FakeWorkspace):
    def __init__(self, fault: _RestorationFault) -> None:
        super().__init__()
        self.fault = fault

    async def snapshot(self, label: str) -> str:
        revision = await super().snapshot(label)
        self.fault.commit()
        return revision

    async def pending_changes(self) -> list[str]:
        return list(self.fault.changes)

    async def restore_for_agent(self, revision: str, *, preserve_paths: tuple[str, ...]) -> None:
        if self.fault.restore(revision, preserve_paths):
            await super().restore_for_agent(revision, preserve_paths=preserve_paths)


class _FaultedRuntimeWorkspace(_WorkspaceResource):
    def __init__(self, fault: _RestorationFault) -> None:
        super().__init__()
        self.fault = fault

    def snapshot(self, label: str) -> str:
        self.fault.commit()
        return super().snapshot(label)

    def pending_changes(self) -> list[str]:
        return list(self.fault.changes)

    def restore(self, revision: str, *, preserve_paths: tuple[str, ...] = (), **_: bool) -> bool:
        # A lying restore exercises post-restore verification, too.
        self.fault.restore(revision, preserve_paths)
        return True


async def _retry(session: AgentSession, message: RenderedPrompt, retry: str) -> None:
    if retry == "resume":
        await session.resume(message, "failed-restore")
    elif retry == "snapshot":
        await session.workspace.snapshot("external-snapshot")
    else:
        await session.turn("next turn")


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("access", [WorkspaceAccess.READ_ONLY, WorkspaceAccess.LIMITED])
@pytest.mark.parametrize("retry", ["resume", "turn", "new-session", "snapshot"])
@pytest.mark.parametrize("failure", ["raise", "leave-dirty"])
def test_failed_resume_restoration_fences_workspace_snapshots(
    implementation: str, access: WorkspaceAccess, retry: str, failure: str, tmp_path: Path
) -> None:
    """A failed restore must not let any caller commit the contaminated tree."""
    fault = _RestorationFault(failure)
    workspace = (
        _FaultedFakeWorkspace(fault)
        if implementation == "fake"
        else _FaultedRuntimeWorkspace(fault)
    )
    grants = ("allowed.json",) if access is WorkspaceAccess.LIMITED else ()
    role = AgentRole(id="worker", system_prompt="Work carefully.", workspace_access=access)
    dispatches: list[str] = []

    def mutate(request: AgentTurnRequest) -> None:
        if request.invocation_id is not None:
            dispatches.append(request.invocation_id)
            fault.changes.extend([*grants, "forbidden.py"])

    transport, client = _resume_transport(tmp_path, mutate)
    message = TemplateRenderer(tmp_path).render_string("trusted result")
    expected_error = WorkspaceRestoreError if failure == "raise" else RuntimeContractError

    async def check() -> None:
        opened = await _open_resume_contract(implementation, role, workspace, transport, grants)
        session = opened.sessions[0]
        try:
            with pytest.raises(expected_error):
                await session.resume(message, "failed-restore")
            outcome = session.inspect("failed-restore")
            assert isinstance(outcome, Completed)
            baseline = fault.attempts[0]
            if retry == "new-session":
                await session.close()
                session = await opened.owner.create_session(
                    role, workspace=session.workspace, member_id="member", writable_paths=grants
                )
            with pytest.raises(expected_error):
                await _retry(session, message, retry)
            assert fault.attempts == [baseline, baseline]
            assert all("forbidden.py" not in commit for commit in fault.commits)
            fault.recovery_allowed = True  # Explicit recovery barrier, independent of time.
            await session.workspace.snapshot("recovered")
            assert fault.attempts == [baseline, baseline, baseline]
            assert fault.commits[-1] == grants
            assert await session.workspace.pending_changes() == []
            assert await session.resume(message, "failed-restore") == outcome
            assert dispatches == ["failed-restore"]
        finally:
            await opened.close()
            client.close()

    asyncio.run(check())


class _RestoreGate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.loop: asyncio.AbstractEventLoop | None = None

    async def wait(self) -> None:
        self.calls += 1
        self.entered.set()
        await self.release.wait()

    def wait_sync(self) -> None:
        assert self.loop is not None
        asyncio.run_coroutine_threadsafe(self.wait(), self.loop).result()


class _BlockedRecoveryFakeWorkspace(_FaultedFakeWorkspace):
    def __init__(self, fault: _RestorationFault, gate: _RestoreGate) -> None:
        super().__init__(fault)
        self.gate = gate

    async def restore_for_agent(self, revision: str, *, preserve_paths: tuple[str, ...]) -> None:
        if self.fault.recovery_allowed:
            await self.gate.wait()
        await super().restore_for_agent(revision, preserve_paths=preserve_paths)


class _BlockedRecoveryRuntimeWorkspace(_FaultedRuntimeWorkspace):
    def __init__(self, fault: _RestorationFault, gate: _RestoreGate) -> None:
        super().__init__(fault)
        self.gate = gate

    def restore(self, revision: str, *, preserve_paths: tuple[str, ...] = (), **_: bool) -> bool:
        if self.fault.recovery_allowed:
            self.gate.wait_sync()
        return super().restore(revision, preserve_paths=preserve_paths)


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_concurrent_workspace_recovery_restores_once(implementation: str, tmp_path: Path) -> None:
    fault = _RestorationFault("raise")
    gate = _RestoreGate()
    workspace = (
        _BlockedRecoveryFakeWorkspace(fault, gate)
        if implementation == "fake"
        else _BlockedRecoveryRuntimeWorkspace(fault, gate)
    )
    role = AgentRole(id="worker", system_prompt="Work.", workspace_access=WorkspaceAccess.READ_ONLY)

    def mutate(request: AgentTurnRequest) -> None:
        if request.invocation_id is not None:
            fault.changes.append("forbidden.py")

    transport, client = _resume_transport(tmp_path, mutate)
    message = TemplateRenderer(tmp_path).render_string("trusted result")

    async def check() -> None:
        opened = await _open_resume_contract(implementation, role, workspace, transport)
        session = opened.sessions[0]
        gate.loop = asyncio.get_running_loop()
        operations: list[asyncio.Task[str]] = []
        entering = asyncio.create_task(gate.entered.wait())
        try:
            with pytest.raises(WorkspaceRestoreError):
                await session.resume(message, "failed-restore")
            fault.recovery_allowed = True
            operations.append(asyncio.create_task(session.workspace.snapshot("first")))
            done, _ = await asyncio.wait(
                (operations[0], entering), return_when=asyncio.FIRST_COMPLETED
            )
            assert entering in done, "snapshot completed before restoring the failed baseline"
            second_entered = asyncio.Event()

            async def second_snapshot() -> str:
                second_entered.set()
                return await session.workspace.snapshot("second")

            operations.append(asyncio.create_task(second_snapshot()))
            await second_entered.wait()
            assert not operations[1].done()
            assert gate.calls == 1
            gate.release.set()
            await asyncio.gather(*operations)
            assert len(fault.attempts) == 2
            assert gate.calls == 1
            assert all("forbidden.py" not in commit for commit in fault.commits)
        finally:
            gate.release.set()
            entering.cancel()
            await asyncio.gather(entering, *operations, return_exceptions=True)
            await opened.close()
            client.close()

    asyncio.run(check())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("access", [WorkspaceAccess.READ_ONLY, WorkspaceAccess.LIMITED])
@pytest.mark.parametrize("failure", ["raise", "leave-dirty"])
@given(
    forbidden=st.lists(st.sampled_from(("a.py", "nested/b.py", "c.py")), min_size=1, unique=True),
    retries=st.integers(min_value=1, max_value=4),
)
def test_failed_turn_keeps_original_baseline_across_repeated_snapshot_retries(
    implementation: str, access: WorkspaceAccess, failure: str, forbidden: list[str], retries: int
) -> None:
    fault = _RestorationFault(failure)
    workspace = (
        _FaultedFakeWorkspace(fault)
        if implementation == "fake"
        else _FaultedRuntimeWorkspace(fault)
    )
    grants = ("allowed.json",) if access is WorkspaceAccess.LIMITED else ()
    role = AgentRole(id="worker", system_prompt="Work.", workspace_access=access)
    expected_error = WorkspaceRestoreError if failure == "raise" else RuntimeContractError

    def mutate() -> None:
        fault.changes.extend([*grants, *forbidden])

    async def check() -> None:
        opened = await _open_session_contract(
            implementation, role, (workspace,), effects=(mutate,), writable_paths=grants
        )
        session = opened.sessions[0]
        try:
            with pytest.raises(expected_error):
                await session.turn("mutate then fail restoration")
            baseline = fault.attempts[0]
            for _ in range(retries):
                with pytest.raises(expected_error):
                    await session.workspace.snapshot("retry")
            assert fault.attempts == [baseline] * (1 + retries)
            assert fault.commits == [()]
            fault.recovery_allowed = True
            await session.workspace.snapshot("recovered")
            assert fault.attempts == [baseline] * (2 + retries)
            assert fault.commits[-1] == grants
        finally:
            await opened.close()

    asyncio.run(check())
