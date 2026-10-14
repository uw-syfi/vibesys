"""A flow runs the task's attribution command and an agent's validation recipes through ``ctx.evaluation``.

Both are operations of the search's namespace that the evidence owner performs. The flow states
the bound, gets the owner's typed outcome or the typed ``Failed``, and a search whose namespace
declares neither cannot ask for them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import (
    BaseModel,  # noqa: TC002  # LW-641209 [TC002]; pydantic evaluates the ClassVar annotation at runtime, so the import cannot move under TYPE_CHECKING.
)

import vs_core.api as core
from vs_core.api.search import search
from vs_core.api.testing import FAKE_OPERATIONS, FlowRun, Interpret, Render, Verify
from vs_sim.api.testing import examples

from .flow_support import Config, open_workspace

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from vs_core.api.search import Flow, NoWinner

REVISION = core.RevisionRef.of_git_commit("revision")


class Capture(core.CaptureCommandBase):
    """The attribution command."""

    kind: Literal["ops.capture_command"] = "ops.capture_command"


class Validate(core.ValidateLocalBase):
    """The validation recipes."""

    kind: Literal["ops.validate_local"] = "ops.validate_local"


class OpsRender(Render):
    """Render a prompt."""

    kind: Literal["ops.render_role_artifacts"] = "ops.render_role_artifacts"


class OpsVerify(Verify):
    """Verify a parent."""

    kind: Literal["ops.verify_parent_revision"] = "ops.verify_parent_revision"


class OpsInterpret(Interpret):
    """Decode evidence."""

    kind: Literal["ops.interpret_evidence"] = "ops.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = Interpret.outcome_model


OPS = core.StrategyOperations(
    "ops", OpsRender, OpsVerify, OpsInterpret, capture=Capture, validate_local=Validate
)


class Seen(core.Value):
    """What the flow was told."""

    printed: str | None = None
    exit_code: int | None = None
    passed: bool | None = None
    failed: str | None = None


COMMAND = core.CaptureSpec(
    revision=REVISION, argv=("attribute", "--fast"), output_argument="--out", timeout_seconds=60
)


async def capture_root(ctx: core.Flow, _config: Config) -> None:
    """Run the attribution command and publish what it printed."""
    found = await ctx.evaluation.capture(COMMAND, within=30.0)
    match found:
        case core.CommandCapture(output=output, exit_code=code):
            ctx.publish(Seen(printed=output, exit_code=code))
        case core.Failed(reason=reason):
            ctx.publish(Seen(failed=reason.value))
        case None:
            ctx.publish(Seen())


async def validate_root(ctx: core.Flow, _config: Config) -> None:
    """Validate a recipe in an opened workspace and publish the verdict."""
    async with open_workspace(ctx, "w") as workspace:
        assert isinstance(workspace, core.Workspace)
        found = await ctx.evaluation.validate_local(
            REVISION, "recipes.json", workspace=workspace, within=30.0
        )
        match found:
            case core.LocalValidationReport(passed=passed):
                ctx.publish(Seen(passed=passed))
            case core.Failed(reason=reason):
                ctx.publish(Seen(failed=reason.value))
            case None:
                ctx.publish(Seen())


def start(
    root: Callable[[Flow, Config], Coroutine[Any, Any, NoWinner | None]],
    *,
    restarting: bool,
    operations: core.StrategyOperations = OPS,
) -> FlowRun[Config]:
    definition = search("ops", config=Config, operations=operations)(root)
    return FlowRun.start(definition, Config(), restarting=restarting)


@pytest.mark.parametrize("restarting", [False, True])
@settings(max_examples=examples(pr=4, nightly=100))
@given(output=st.text(max_size=20), code=st.integers(0, 3))
def test_the_attribution_command_is_run_with_the_argv_and_bounds_the_flow_stated(
    *, restarting: bool, output: str, code: int
) -> None:
    run = start(capture_root, restarting=restarting)
    asked = run.expect(core.Operation)
    request = asked.request.request  # ty: ignore[unresolved-attribute]
    assert isinstance(request, Capture)
    assert (request.argv, request.output_argument, request.timeout_seconds, request.revision) == (
        COMMAND.argv,
        COMMAND.output_argument,
        COMMAND.timeout_seconds,
        REVISION,
    )
    assert asked.until is not None
    assert asked.until.limit == 30.0
    run.finish_operation(
        asked, core.CommandCapture(status="succeeded", exit_code=code, output=output)
    )
    assert run.published(Seen) == Seen(printed=output, exit_code=code)


@pytest.mark.parametrize("restarting", [False, True])
@pytest.mark.parametrize("reason", [core.FailureReason.TIMED_OUT, core.FailureReason.LOST])
def test_an_attribution_that_ends_without_a_result_is_the_typed_failure(
    *, restarting: bool, reason: core.FailureReason
) -> None:
    run = start(capture_root, restarting=restarting)
    run.fail(run.expect(core.Operation), reason)
    assert run.published(Seen) == Seen(failed=reason.value)


@pytest.mark.parametrize("restarting", [False, True])
@given(passed=st.booleans())
def test_validation_recipes_run_in_the_workspace_scope_against_the_revision(
    *, restarting: bool, passed: bool
) -> None:
    run = start(validate_root, restarting=restarting)
    workspace = run.open_workspace(run.expect(core.StartAttempt))
    asked = run.expect(core.Operation)
    request = asked.request.request  # ty: ignore[unresolved-attribute]
    assert isinstance(request, Validate)
    assert (request.revision, request.recipe_artifact) == (REVISION, "recipes.json")
    assert asked.request.scope.owner == workspace.attempt_id
    run.finish_operation(asked, core.LocalValidationReport(status="succeeded", passed=passed))
    assert run.published(Seen) == Seen(passed=passed)


def test_a_namespace_without_capture_refuses_to_ask_for_it() -> None:
    definition = search("ops", config=Config, operations=FAKE_OPERATIONS)(capture_root)
    with pytest.raises(core.ContractError):
        FlowRun.start(definition, Config())
