"""Owner of command capture, over public ``Workspaces`` and ``CaptureCommands`` interfaces.

A profile-guided strategy runs a configured profiler command against a retained revision and
reads what it wrote. The command runs in a throwaway isolated candidate created from that
revision, through the runtime's own output capture (it appends the output argument and a
temporary path, bounds the output and removes the file), and the candidate is discarded on
every settled path. If a remote stop cannot be confirmed, the candidate is durably retained
across host cleanup and restart rather than being removed under a possibly live process.
Nothing durable changes on a settled path, so the operation is a query. The candidate is created
under a member identity derived from the request's identity, so the worktree a host that died
mid-capture leaves behind is replaced by the replay's, never accumulated; inspection proves
nothing happened (the executor seals a finished result, so a finished capture is never rerun)
and the request is executed again. A revision the run does not retain is refused as an outcome, not an error; a
command that cannot run at all (the sandbox failed) propagates as an infrastructure failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from vs_runtime._operation_catalog import Inspection, NotApplied
from vs_runtime.contracts import CaptureStopUnconfirmedError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_core.api import OperationRequest, RevisionRef
    from vs_runtime._core_requests import ExecutionContext
    from vs_runtime.contracts import CaptureCommands, RevisionLedger, Workspaces


class CaptureRequest(Protocol):
    """The request shape this owner serves, declared by the strategy that issues it."""

    revision: RevisionRef
    argv: tuple[str, ...]
    output_argument: str
    timeout_seconds: int


class CaptureCommandOwner:
    """Run a configured argv in an isolated workspace at a retained revision."""

    def __init__(
        self,
        workspaces: Workspaces,
        commands: CaptureCommands,
        ledger: RevisionLedger,
        commit_of: Callable[[RevisionRef], str | None],
    ) -> None:
        """Bind the workspaces, the command runner, the retention ledger and the encoding."""
        self._workspaces = workspaces
        self._commands = commands
        self._ledger = ledger
        self._commit_of = commit_of

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> Mapping[str, object]:
        """Capture the command's output at the revision; infrastructure errors raise."""
        return await self.capture(request, context.payload_digest)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        """Inspection never runs the command: a query leaves no effect to prove."""
        del request, context
        return NotApplied()

    async def capture(self, request: OperationRequest, identity: str) -> Mapping[str, object]:
        """Run the command at the revision, or say why not.

        ``identity`` names this execution (the executed request's payload digest): the same
        identity always uses the same candidate path.
        """
        asked = cast("CaptureRequest", request)
        commit = self._commit_of(asked.revision)
        if commit is None:
            return _refused("revision reference is not canonical")
        if not await self._ledger.retains(commit):
            return _refused("revision is not retained by the run workspace")
        if not self._workspaces.supports_parallel_candidates:
            return _refused("the run has no isolated candidate workspaces to run the command in")
        candidate = await self._workspaces.create_candidate(
            commit, member_id=f"capture-{identity[:32]}"
        )
        settled = True
        try:
            result = await self._commands.capture_output(
                asked.argv,
                workspace=candidate,
                output_argument=asked.output_argument,
                timeout_seconds=asked.timeout_seconds,
            )
        except CaptureStopUnconfirmedError:
            settled = False
            raise
        finally:
            if settled:
                await candidate.discard()
        output = result.output if result.exit_code == 0 else result.diagnostic_output
        return {
            "status": "succeeded",
            "exit_code": result.exit_code,
            "output": output,
            "truncated": result.truncated,
        }


def _refused(detail: str) -> Mapping[str, object]:
    return {"status": "rejected", "detail": detail}
