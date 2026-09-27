"""Public composition contracts for runtime-owned workspace resources."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import create_workspaces, resolve_workspace_resource
from vs_runtime.api.testing import FakeWorkspace

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api import Workspace


class _Resource:
    def __init__(self, identifier: str | None, path: Path, revision: str = "a" * 40) -> None:
        self.id = identifier
        self.path = path
        self.revision: str | None = revision
        self.trusted_input_baseline: str | None = revision
        self.closed = False

    def snapshot(self, label: str) -> str:
        self.revision = f"{len(label):040x}"
        return self.revision

    def restore(
        self,
        revision: str,
        *,
        clean: bool,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> bool:
        del clean, preserve_paths, preserve_memory
        self.revision = revision
        return True

    def try_restore(self, revision: str, *, clean: bool) -> bool:
        return self.restore(revision, clean=clean)

    def retain(self, revision: str, reference: str) -> None:
        del revision, reference

    def pending_changes(self) -> list[str]:
        return []

    def candidate_patch(self, revision: str) -> str:
        return revision

    def trusted_input_changes(self) -> list[str]:
        return []

    def is_directory(self, path: str) -> bool:
        return (self.path / path).is_dir()

    def close(self) -> None:
        self.closed = True


class _Provider:
    supports_parallel_candidates = True

    def __init__(self, path: Path) -> None:
        self.root = _Resource(None, path)
        self.created: list[_Resource] = []
        self.cleanup: list[str] = []

    def create_candidate(self, workspace_id: str, revision: str) -> _Resource:
        resource = _Resource(workspace_id, self.root.path / workspace_id, revision)
        self.created.append(resource)
        return resource

    async def close_sessions(self, workspace: Workspace) -> None:
        self.cleanup.append(f"sessions:{workspace.id}")


def test_collection_owns_candidate_cleanup_in_reverse_order(tmp_path: Path) -> None:
    provider = _Provider(tmp_path)

    async def exercise() -> None:
        workspaces = create_workspaces(provider)
        first = await workspaces.create_candidate()
        second = await workspaces.create_candidate()
        first_id = first.id
        second_id = second.id
        await workspaces.close()
        assert provider.cleanup == [f"sessions:{second_id}", f"sessions:{first_id}"]
        assert all(resource.closed for resource in provider.created)
        assert provider.root.closed
        with pytest.raises(ValueError, match="closed"):
            _ = first.path
        with pytest.raises(ValueError, match="closed"):
            _ = workspaces.root.path

    asyncio.run(exercise())


def test_close_attempts_every_candidate_and_aggregates_failures(tmp_path: Path) -> None:
    class _FailingResource(_Resource):
        def close(self) -> None:
            self.closed = True
            raise OSError(self.id)

    class _FailingProvider(_Provider):
        def create_candidate(self, workspace_id: str, revision: str) -> _Resource:
            resource = _FailingResource(workspace_id, self.root.path / workspace_id, revision)
            self.created.append(resource)
            return resource

    provider = _FailingProvider(tmp_path)

    async def exercise() -> None:
        workspaces = create_workspaces(provider)
        await workspaces.create_candidate()
        await workspaces.create_candidate()
        with pytest.raises(BaseExceptionGroup) as captured:
            await workspaces.close()
        assert len(captured.value.exceptions) == 2
        assert all(resource.closed for resource in provider.created)
        assert len(provider.cleanup) == 2

    asyncio.run(exercise())


def test_workspace_mutations_are_serialized(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _BlockingResource(_Resource):
        def __init__(self, identifier: str | None, path: Path) -> None:
            super().__init__(identifier, path)
            self.calls = 0
            self.active = 0
            self.maximum_active = 0

        def snapshot(self, label: str) -> str:
            self.calls += 1
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.calls == 1:
                entered.set()
                release.wait()
            try:
                return super().snapshot(label)
            finally:
                self.active -= 1

    provider = _Provider(tmp_path)
    root = _BlockingResource(None, tmp_path)
    provider.root = root

    async def exercise() -> None:
        workspaces = create_workspaces(provider)
        first = asyncio.create_task(workspaces.root.snapshot("first"))
        await asyncio.to_thread(entered.wait)
        second_scheduled = asyncio.Event()

        async def second_snapshot() -> str:
            second_scheduled.set()
            return await workspaces.root.snapshot("second")

        second = asyncio.create_task(second_snapshot())
        await second_scheduled.wait()
        assert root.calls == 1
        release.set()
        await asyncio.gather(first, second)
        assert root.maximum_active == 1
        await workspaces.close()

    asyncio.run(exercise())


def test_collection_rejects_foreign_handles_and_early_discard_is_idempotent(
    tmp_path: Path,
) -> None:
    provider = _Provider(tmp_path)

    async def exercise() -> None:
        workspaces = create_workspaces(provider)
        candidate = await workspaces.create_candidate()
        candidate_id = candidate.id
        with pytest.raises(TypeError, match="live handle"):
            resolve_workspace_resource(workspaces, FakeWorkspace(path=tmp_path))
        await candidate.discard()
        await candidate.discard()
        assert provider.cleanup == [f"sessions:{candidate_id}"]
        await workspaces.close()

    asyncio.run(exercise())


def test_cancelled_candidate_construction_drains_and_closes_partial_resource(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class _BlockingProvider(_Provider):
        def create_candidate(self, workspace_id: str, revision: str) -> _Resource:
            started.set()
            release.wait()
            return super().create_candidate(workspace_id, revision)

    provider = _BlockingProvider(tmp_path)

    async def exercise() -> None:
        workspaces = create_workspaces(provider)
        construction = asyncio.create_task(workspaces.create_candidate())
        await asyncio.to_thread(started.wait)
        construction.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await construction
        assert len(provider.created) == 1
        assert provider.created[0].closed
        await workspaces.close()

    asyncio.run(exercise())
