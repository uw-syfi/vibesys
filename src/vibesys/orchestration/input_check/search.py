"""Measure a task's unchanged input with its configured gates, without agents."""

from vibesys.candidates.api import Baseline, failure, measure_input, trusted
from vibesys.orchestration.input_check.operations import OPERATIONS
from vs_core.api.search import INPUT, Flow, NoWinner, Value, search


class InputCheckConfig(Value):
    """The input check has no search knobs or agent budget."""


class InputCheckSnapshot(Value):
    """The durable result of checking the input revision."""

    baseline: Baseline


def failure_reason(baseline: Baseline) -> str:
    """Describe why the task's unchanged input did not pass its configured gates."""
    failed = failure(baseline)
    if failed is None:
        return "the input did not produce a trusted gate result"
    details = [failed.kind.value]
    if failed.status is not None:
        details.append(f"status={failed.status.value}")
    if failed.ended is not None:
        details.append(f"ended={failed.ended.value}")
    if failed.cause is not None:
        details.append(f"cause={failed.cause.value}")
    return f"the input did not pass its configured gates ({', '.join(details)})"


@search("check-input", config=InputCheckConfig, operations=OPERATIONS)
async def check_input(ctx: Flow, _config: InputCheckConfig) -> NoWinner | None:
    """Run the task's gates once against the trusted input and finish."""
    baseline = await measure_input(ctx)
    ctx.publish(InputCheckSnapshot(baseline=baseline))
    if not trusted(baseline):
        return NoWinner(reason=failure_reason(baseline))
    ctx.choose(INPUT)
    return None


__all__ = [
    "InputCheckConfig",
    "InputCheckSnapshot",
    "check_input",
    "failure_reason",
]
