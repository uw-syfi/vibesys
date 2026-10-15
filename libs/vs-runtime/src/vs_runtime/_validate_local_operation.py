"""Owner of local validation, over the public ``Workspaces`` and ``LocalValidator`` interfaces.

An agent writes a recipe file (bounded trusted commands that check its own candidate) and
reports its path. The strategy asks for the recipes to run against the candidate's retained
revision. They run in a throwaway isolated candidate created from exactly that revision, through
the runtime's trusted-shell command surface, and a recipe that changes the tree fails. The
workspace is discarded on every path and the JSON report of every recipe's result is stored as a
content-addressed artifact, which the outcome names. Nothing in the run's workspaces changes, so
the operation is a query: inspection proves nothing happened (a finished result is sealed by the
executor) and the request is executed again. The candidate is created under a member identity
derived from the request's identity, so the worktree a host that died mid-run leaves behind is
replaced by the replay's.

A revision the run does not retain, a run without isolated candidates and a recipe reference that
cannot name a file are refused as outcomes; a recipe file that is missing or malformed is the
agent's input error (``recipe_unusable``), not a failed candidate; a sandbox that cannot run at
all propagates as an infrastructure failure.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol, cast

from vs_core.api import ArtifactId, ArtifactRef
from vs_runtime._local_validation import (
    FrameworkValidationResult,
    LocalValidationRecipeError,
    check_recipe_artifact_path,
)
from vs_runtime._operation_catalog import Inspection, NotApplied

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_core.api import OperationRequest, RevisionRef
    from vs_runtime._artifact_store import ArtifactStore
    from vs_runtime._core_requests import ExecutionContext
    from vs_runtime._local_validation import LocalValidationEvents, LocalValidator
    from vs_runtime.contracts import RevisionLedger, Workspaces

# Where the report is written inside the throwaway workspace before it is stored.
_REPORT_LOCATION = "validation-report.json"


class ValidateRequest(Protocol):
    """The request shape this owner serves, declared by the strategy that issues it."""

    revision: RevisionRef
    recipe_artifact: str


class ValidateLocalOwner:
    """Run an agent-authored recipe file in an isolated workspace at a retained revision."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-731907 [PLR0913]; the owner's five collaborators are independent seams a config object would only rename.
        self,
        workspaces: Workspaces,
        validator: LocalValidator,
        ledger: RevisionLedger,
        commit_of: Callable[[RevisionRef], str | None],
        store: ArtifactStore,
        events: LocalValidationEvents | None = None,
    ) -> None:
        """Bind the workspaces, the recipe runner, the retention ledger, the encoding and the store."""
        self._workspaces = workspaces
        self._validator = validator
        self._ledger = ledger
        self._commit_of = commit_of
        self._store = store
        self._events = events

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> Mapping[str, object]:
        """Run the recipes at the revision; infrastructure errors raise."""
        return await self.validate(request, context.payload_digest)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        """Inspection never runs a recipe: a query leaves no effect to prove."""
        del request, context
        return NotApplied()

    async def validate(self, request: OperationRequest, identity: str) -> Mapping[str, object]:
        """Run the recipes at the revision, or say why not.

        ``identity`` names this execution (the executed request's payload digest): the same
        identity always uses the same candidate path.
        """
        asked = cast("ValidateRequest", request)
        commit = self._commit_of(asked.revision)
        if commit is None:
            return _refused("revision reference is not canonical")
        if not await self._ledger.retains(commit):
            return _refused("revision is not retained by the run workspace")
        if not self._workspaces.supports_parallel_candidates:
            return _refused("the run has no isolated candidate workspaces to run the recipes in")
        try:
            check_recipe_artifact_path(asked.recipe_artifact)
        except LocalValidationRecipeError as error:
            return _unusable(str(error))
        candidate = await self._workspaces.create_candidate(
            commit, member_id=f"validate-{identity[:32]}"
        )
        try:
            try:
                results = await self._validator.run(
                    candidate,
                    recipe_artifact=asked.recipe_artifact,
                    report_location=_REPORT_LOCATION,
                    events=self._events,
                )
            except LocalValidationRecipeError as error:
                return _unusable(str(error))
            report = self._store.write(_report_bytes(results))
        finally:
            await candidate.discard()
        return _outcome(results, report.sha256)


def _report_bytes(results: tuple[FrameworkValidationResult, ...]) -> bytes:
    payload = json.dumps(
        {"version": 1, "results": [item.model_dump(mode="json") for item in results]},
        indent=2,
        sort_keys=True,
    )
    return f"{payload}\n".encode()


def _outcome(results: tuple[FrameworkValidationResult, ...], digest: str) -> Mapping[str, object]:
    failed = next((item for item in results if not item.passed), None)
    return {
        "status": "succeeded",
        "passed": failed is None,
        "recipe": "" if failed is None else failed.recipe.name,
        "error": "" if failed is None else (failed.error or "unknown failure"),
        "output": "" if failed is None else failed.output,
        "report": ArtifactRef(artifact_id=ArtifactId(root=f"validation:{digest}"), digest=digest),
    }


def _unusable(detail: str) -> Mapping[str, object]:
    return {"status": "succeeded", "passed": False, "recipe_unusable": True, "error": detail}


def _refused(detail: str) -> Mapping[str, object]:
    return {"status": "rejected", "detail": detail}
