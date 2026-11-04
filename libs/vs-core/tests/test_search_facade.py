"""``vs_core.api.search`` is the search contract and only that.

A search imports this module and nothing else from vs-core. It holds what a search writes
(``@search``, the roles it asks, the values its aspects take) and what it reads (events,
``Failed``, ``RunView``), and keeps out the plumbing that belongs to core and the shell.
"""

import vs_core.api as shell_facade
import vs_core.api.search as search_facade
from vs_core.api.search import AttemptSummary

PLUMBING = {
    "RequestRecord",
    "RequestPhase",
    "Outbound",
    "Until",
    "within",
    "inherit",
    "advance",
    "begin",
    "Inbox",
    "Advanced",
    "search_events",
    "translate",
    "Translated",
    "Discarded",
    "DiscardReason",
    "check_decisions",
    "SearchHost",
    "OpenWait",
    "step",
    "initial_state",
    "CoreState",
    "RequestId",
}


def test_the_search_facade_exports_every_name_it_lists_and_nothing_of_core_plumbing() -> None:
    assert set(search_facade.__all__) == {
        name for name in dir(search_facade) if not name.startswith("_")
    } - {"annotations"}
    assert PLUMBING.isdisjoint(search_facade.__all__)
    assert set(shell_facade.__all__) >= PLUMBING, "the plumbing is still there for the shell"


def test_the_facade_names_are_the_shell_facades_own_objects() -> None:
    # `vs_core.api.search` is also a module of that package, so `search` itself is the one name
    # the shell facade does not carry.
    for name in set(search_facade.__all__) - {"search"}:
        assert getattr(search_facade, name) is getattr(shell_facade, name), name


def test_a_search_can_write_a_decision_and_read_a_failure_with_only_the_facade() -> None:
    names = set(search_facade.__all__)
    assert {"search", "Flow", "Role", "Session", "Within", "NoWinner", "INPUT", "Kept"} <= names
    assert {"WhileOpen", "WhileAlive", "Failed", "FailureReason", "RunView", "Assessment"} <= names
    assert {"RequestTurn", "StartAttempt", "Withdraw", "Measure", "ProposeWinner"} <= names
    assert {"WorkspaceOpened", "WorkspaceClosed", "TurnFinished", "MeasurementFinished"} <= names


def test_a_failure_names_the_decision_and_no_request() -> None:
    assert "request" not in search_facade.Failed.model_fields
    assert "decision" in search_facade.Failed.model_fields


def test_what_a_search_may_read_of_a_workspace_has_no_request_ids_or_receipts() -> None:
    fields = set(AttemptSummary.model_fields)
    assert fields.isdisjoint(
        {"pending_records", "charges", "release_dependencies", "checkpoints", "admission_id"}
    )
    assert search_facade.RunView.model_fields["attempts"].annotation == tuple[AttemptSummary, ...]


def test_every_core_export_loads_as_the_same_public_object() -> None:
    for name in shell_facade.__all__:
        assert getattr(shell_facade, name) is getattr(shell_facade, name)
    assert dir(shell_facade) == sorted(shell_facade.__all__)
