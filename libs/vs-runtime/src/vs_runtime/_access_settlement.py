"""Judge each invocation's workspace writes exactly once, when its writer is proven ended.

An invocation gets an ``AccessReceipt`` before its provider runs: the workspace
baseline and the role's grant. The receipt is *settled* once the workspace provably
holds only what the grant allows. Every executor that proves a writer ended
(a completed or invalid turn, a cancel that released it, a close, a resume that
superseded it) calls ``AccessSettlement.settle``; one that cannot prove it calls
``fence``. Run-invocation proofs read ``unproven``, so a writer that ended but was
never judged cannot be reported as ended.

Until settled, an invocation that is no longer in flight in this process keeps its
workspace fenced: the workspace refuses to snapshot, and no new turn may baseline it,
because that would adopt the unjudged writes. The fence is the durable record written
with the receipt, before the provider can run, and cleared only by the settlement, so
it holds across a host restart before any executor has seen the invocation again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from vs_core.api import Scope, WorkspaceRef
from vs_project.api import NestedRepositoryError
from vs_runtime._session_operation import await_session_operation
from vs_runtime._workspace_access import (
    AccessGrant,
    AccessNotRestoredError,
    enforce_workspace_access,
)
from vs_runtime.contracts import RuntimeContractError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._workspace_access import AccessGuardedWorkspace

_ACCESS = "session-access"
_UNSETTLED = "session-access-unsettled"

type WorkspaceLookup = Callable[[WorkspaceRef | Scope], Awaitable[AccessGuardedWorkspace | None]]


class AccessSettlementError(RuntimeContractError):
    """Access cannot be settled or a new baseline cannot be taken yet; retry after it is."""


class AccessUnrestorableError(AccessSettlementError):
    """A turn's writes cannot be judged or undone; retrying cannot help.

    Either they survived a complete revert, or the workspace holds a nested repository
    that host Git refuses to read, so no snapshot or diff of it can be taken.

    The workspace stays fenced, so no later turn can take those writes as its baseline,
    and every turn that needs the workspace is refused for good, naming the paths.
    """


class AccessViolation(BaseModel):
    """A turn wrote outside its role's workspace access; the writes were reverted."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    role_id: str
    paths: tuple[str, ...]


class AccessReceipt(BaseModel):
    """Written before the provider is called: how to undo what the turn may write.

    The baseline is the workspace snapshot taken before the turn. It outlives a host
    crash, so a replay or an inspection reverts the turn's unauthorized writes from
    the same baseline instead of taking the tainted tree as a new one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace: WorkspaceRef | Scope
    grant: AccessGrant
    baseline: str
    workspace_path: str
    """The workspace's host path: the identity the durable fence is keyed by."""
    violation: AccessViolation | None = None
    """Recorded before the revert starts, so a crash during it cannot lose the finding."""
    settled: bool = False
    """True once the workspace provably holds only what the grant allows."""
    unrestorable: tuple[str, ...] = ()
    """Paths a complete revert left behind (see ``AccessUnrestorableError``)."""


class AccessKey(BaseModel):
    """One invocation of one bound session: where its access receipt lives."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    binding: str
    """The session binding key (``session_binding_key``)."""
    invocation: str

    @property
    def text(self) -> str:
        """The key under which the receipt and the fence are stored."""
        return f"{self.binding}/{self.invocation}"


class UnsettledInvocations(BaseModel):
    """Invocations of one workspace whose writes are not yet judged."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    keys: tuple[AccessKey, ...] = ()


class AccessSettlement:
    """The one place that begins, settles and fences invocation access receipts."""

    def __init__(self, store: ReceiptStore, workspace_for: WorkspaceLookup) -> None:
        """Bind the shared receipt store and the run's workspace lookup."""
        self._store = store
        self._workspace_for = workspace_for
        self._in_flight: set[AccessKey] = set()

    def receipt(self, key: AccessKey) -> AccessReceipt | None:
        """The invocation's receipt, or None before its turn began."""
        return self._store.load(_ACCESS, "access", key.text, AccessReceipt)

    def unproven(self, key: AccessKey) -> str | None:
        """None when the invocation's writes were judged; otherwise why they were not."""
        return access_unproven(self._store, key)

    def blocking(self, workspace: Path, key: AccessKey | None = None) -> tuple[AccessKey, ...]:
        """Invocations of the workspace at *workspace* that are unsettled and not in flight here.

        An invocation still in flight in this process is a live peer, not a fence.
        One that is not (its call returned Unknown, or its host died) may have left
        unjudged writes that a new snapshot would adopt. Read from the durable record,
        so a restarted host sees them before any executor has. *key* is excluded.
        """
        return tuple(
            other
            for other in self._unsettled(workspace)
            if other != key and other not in self._in_flight
        )

    def fenced_by(self, workspace: Path) -> tuple[str, ...]:
        """The invocations that keep the workspace at *workspace* from snapshotting."""
        return tuple(other.text for other in self.blocking(workspace))

    def require_clear(self, workspace: Path, key: AccessKey) -> None:
        """Raise unless ``blocking`` is empty: no new baseline may adopt unjudged writes."""
        blocking = self.blocking(workspace, key)
        stuck = [
            (other, receipt)
            for other in blocking
            if (receipt := self.receipt(other)) is not None and receipt.unrestorable
        ]
        if stuck:
            raise AccessUnrestorableError(
                "the workspace holds writes outside a turn's access that could not be undone: "
                + "; ".join(
                    f"{receipt.grant.role_id} ({other.text}): {', '.join(receipt.unrestorable)}"
                    for other, receipt in stuck
                )
            )
        if blocking:
            message = (
                "the workspace has invocations whose writes are not judged: "
                f"{', '.join(other.text for other in blocking)}; settle them first"
            )
            raise AccessSettlementError(message)

    async def begin(
        self,
        key: AccessKey,
        workspace_ref: WorkspaceRef | Scope,
        workspace: AccessGuardedWorkspace,
        grant: AccessGrant,
    ) -> None:
        """Snapshot the workspace once, durably, before the turn can write to it.

        The receipt and the durable fence entry are one write, and it completes
        before this returns, so before any dispatch can start.
        """
        self._in_flight.add(key)
        begun = False
        try:
            receipt = self.receipt(key)
            if receipt is not None:
                # A replay keeps the first baseline, never the tree the turn left. Repair
                # an interrupted first begin that wrote the receipt before its workspace
                # index; no provider call could have followed a begin that did not return.
                if not receipt.settled:
                    self._track(Path(receipt.workspace_path), key, add=True)
                begun = True
                return
            self.require_clear(workspace.path, key)
            try:
                baseline = await await_session_operation(
                    asyncio.create_task(workspace.snapshot(f"{key.text}-input"))
                )
            except NestedRepositoryError as error:
                raise AccessUnrestorableError(_nested_message(error)) from error
            except RuntimeContractError as error:
                raise AccessSettlementError(str(error)) from error
            receipt = AccessReceipt(
                workspace=workspace_ref,
                grant=grant,
                baseline=baseline,
                workspace_path=str(workspace.path),
            )

            def keep_first(stored: AccessReceipt | None) -> tuple[AccessReceipt | None, None]:
                return (receipt if stored is None else None), None

            with self._store.exclusive():
                self._store.modify(_ACCESS, "access", key.text, AccessReceipt, keep_first)
                self._track(workspace.path, key, add=True)
            begun = True
        finally:
            if not begun:
                self._in_flight.discard(key)

    def fence(self, key: AccessKey) -> None:
        """End this host's live ownership, exposing any remaining durable fence.

        The record written by ``begin`` keeps the workspace out of snapshots once
        the invocation is no longer in flight here. ``settle`` is the only operation
        that clears that durable record, so this is safe after every post-begin exit,
        including successful settlement, cancellation, and an unexpected exception.
        """
        self._in_flight.discard(key)

    async def settle(self, key: AccessKey) -> AccessViolation | None:
        """Revert the unauthorized writes of a writer proven ended; its violation, if any.

        Idempotent: a settled receipt returns its recorded violation without touching
        the workspace. Raises ``AccessSettlementError`` when the revert cannot finish;
        the workspace then stays fenced and a retry resumes the same restoration.
        """
        # Settlement is terminal for this host's writer ownership. Release it before
        # any lookup, workspace I/O, or receipt write can fail; the durable receipt then
        # remains visible to snapshots and peer turns in this same process.
        self._in_flight.discard(key)
        receipt = self.receipt(key)
        if receipt is None:
            return None
        if receipt.settled:
            # A prior call may have recorded settlement and then failed while removing
            # the workspace index. Retrying finishes that release idempotently.
            self._track(Path(receipt.workspace_path), key, add=False)
            return receipt.violation
        workspace = await self._workspace_for(receipt.workspace)
        if workspace is None:
            message = "the turn's workspace is gone before its access settled"
            raise AccessSettlementError(message)

        def record(paths: list[str]) -> None:
            found = AccessViolation(role_id=receipt.grant.role_id, paths=tuple(paths))
            self._store.replace(
                _ACCESS, "access", key.text, receipt.model_copy(update={"violation": found})
            )

        try:
            await await_session_operation(
                asyncio.create_task(
                    enforce_workspace_access(
                        workspace, receipt.grant, receipt.baseline, observer=record
                    )
                )
            )
        except AccessNotRestoredError as error:
            self._store.replace(
                _ACCESS,
                "access",
                key.text,
                (self.receipt(key) or receipt).model_copy(update={"unrestorable": error.paths}),
            )
            message = f"workspace access could not be restored: {error}"
            raise AccessUnrestorableError(message) from error
        except NestedRepositoryError as error:
            # The turn ended and left a repository host Git must not read, so its writes
            # can be neither read nor reverted, and reading again finds the same tree:
            # this workspace is fenced for good, and the turn alone fails.
            self._store.replace(
                _ACCESS,
                "access",
                key.text,
                (self.receipt(key) or receipt).model_copy(
                    update={"unrestorable": error.paths or (_UNCHECKABLE_TREE,)}
                ),
            )
            raise AccessUnrestorableError(_nested_message(error)) from error
        except RuntimeContractError as error:
            message = f"workspace access is not restored: {error}"
            raise AccessSettlementError(message) from error
        with self._store.exclusive():
            latest = self.receipt(key) or receipt
            self._store.replace(
                _ACCESS, "access", key.text, latest.model_copy(update={"settled": True})
            )
            self._track(Path(receipt.workspace_path), key, add=False)
        return latest.violation

    def _unsettled(self, workspace: Path) -> tuple[AccessKey, ...]:
        stored = self._store.load(
            _UNSETTLED, "workspace", _workspace_key(workspace), UnsettledInvocations
        )
        return () if stored is None else stored.keys

    def _track(self, workspace: Path, key: AccessKey, *, add: bool) -> None:
        def decide(
            stored: UnsettledInvocations | None,
        ) -> tuple[UnsettledInvocations | None, None]:
            keys = () if stored is None else stored.keys
            kept = tuple(k for k in keys if k != key)
            return UnsettledInvocations(keys=(*kept, key) if add else kept), None

        self._store.modify(
            _UNSETTLED, "workspace", _workspace_key(workspace), UnsettledInvocations, decide
        )


def access_unproven(store: ReceiptStore, key: AccessKey) -> str | None:
    """None when the invocation's receipt is settled; otherwise why its writes are unjudged.

    The one check behind every run-invocation proof, so "ended" cannot be reported
    for a writer whose writes were never judged.
    """
    receipt = store.load(_ACCESS, "access", key.text, AccessReceipt)
    if receipt is None:
        return "the invocation has no access receipt, so its writes were never judged"
    if not receipt.settled:
        return "the turn's workspace access has not been settled yet"
    return None


_UNCHECKABLE_TREE = "(the work tree is too large to check)"


def _nested_message(error: NestedRepositoryError) -> str:
    return f"the workspace cannot be read by host Git: {error}"


def _workspace_key(workspace: Path) -> str:
    return str(workspace)


__all__ = [
    "AccessKey",
    "AccessReceipt",
    "AccessSettlement",
    "AccessSettlementError",
    "AccessViolation",
    "UnsettledInvocations",
    "access_unproven",
]
