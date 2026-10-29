"""Published test drivers for code that runs a strategy against core.

A fake world answers core's requests (`Answer`), a `Harness` fixes the run's facts, and a
`Journal` records what a run did (`liveness` checks it against the run's invariants). `drive`
runs a search against a `Script` of answers and `Faults`, `FlowRun` and `fake_search` run a flow
against scripted outcomes, with `FAKE_OPERATIONS` as the operation shapes of a toy namespace.
They are the pieces a host-side test kit builds on, so a strategy's tests and the runtime's shell
tests answer requests the same way.
"""

from vs_core.testing.builders import sample_limits, sample_retries
from vs_core.testing.drive import (
    Answer,
    Failed,
    Faults,
    Harness,
    Retryable,
    Running,
    Script,
    Succeeded,
    Trace,
    Unknown,
    drive,
    new_run,
)
from vs_core.testing.flows import (
    FAKE_OPERATIONS,
    FAKE_REGISTRY,
    Asked,
    FakeView,
    FlowRun,
    Interpret,
    Prompt,
    Render,
    RestartPolicy,
    Verify,
    code_changed,
    fake_search,
)
from vs_core.testing.liveness import Budget, End, Journal, Violation, assert_live, liveness
from vs_core.testing.persisted_schema import (
    Incompatibility,
    SchemaSnapshot,
    assert_schema_snapshot,
    parse_schema_snapshot,
    schema_incompatibilities,
    schema_snapshot,
    snapshot_problems,
)
from vs_core.testing.schema_versions import assert_schema_pinned, schema_digest

__all__ = [
    "FAKE_OPERATIONS",
    "FAKE_REGISTRY",
    "Answer",
    "Asked",
    "Budget",
    "End",
    "Failed",
    "FakeView",
    "Faults",
    "FlowRun",
    "Harness",
    "Incompatibility",
    "Interpret",
    "Journal",
    "Prompt",
    "Render",
    "RestartPolicy",
    "Retryable",
    "Running",
    "SchemaSnapshot",
    "Script",
    "Succeeded",
    "Trace",
    "Unknown",
    "Verify",
    "Violation",
    "assert_live",
    "assert_schema_pinned",
    "assert_schema_snapshot",
    "code_changed",
    "drive",
    "fake_search",
    "liveness",
    "new_run",
    "parse_schema_snapshot",
    "sample_limits",
    "sample_retries",
    "schema_digest",
    "schema_incompatibilities",
    "schema_snapshot",
    "snapshot_problems",
]
