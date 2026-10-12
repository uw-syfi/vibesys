"""Public iteration projection invariants and built-in catalog coverage."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from launch.catalog import built_in_orchestrations
from vibesys.api import (
    IterationKind,
    IterationStatus,
    IterationSummary,
    IterationSupport,
    IterationTree,
    catalog,
)
from vibesys.orchestration.dynamic.core_policy.api import project_strategy_state
from vibesys.orchestration.dynamic.strategy.api import (
    AttemptRecord,
    DynamicStrategyState,
    WorkKind,
    WorkPhase,
    WorkPlan,
)
from vibesys.orchestration.issue_queue.core_policy.api import (
    project_strategy_state as project_issue_queue_state,
)
from vibesys.orchestration.issue_queue.strategy.api import BoardState, IssueQueueStrategyState
from vs_core.api import AttemptId, RevisionRef
from vs_issue_tracker.api import IssueRow, IssueStatus, IssueType


def _node(identifier: str, ordinal: int) -> IterationSummary:
    return IterationSummary(
        id=identifier,
        kind=IterationKind.ROUND,
        label=identifier,
        ordinal=ordinal,
        status=IterationStatus.COMPLETED,
    )


@given(st.lists(st.text(min_size=1), min_size=1, max_size=20, unique=True))
def test_revision_is_stable_for_identical_nodes(identifiers: list[str]) -> None:
    nodes = tuple(_node(identifier, ordinal) for ordinal, identifier in enumerate(identifiers))

    first = IterationTree.from_nodes(nodes)
    replayed = IterationTree.model_validate_json(first.model_dump_json())

    assert replayed == first
    assert IterationTree.from_nodes(nodes).revision == first.revision
    assert IterationTree.from_nodes(tuple(reversed(nodes))).revision == first.revision


@pytest.mark.parametrize(
    ("nodes", "path"),
    [
        ((_node("same", 0), _node("same", 1)), "nodes[1].id"),
        (
            (
                IterationSummary(
                    id="child",
                    parent_id="missing",
                    kind=IterationKind.CANDIDATE,
                    label="child",
                    ordinal=0,
                    status=IterationStatus.PENDING,
                ),
            ),
            "nodes[0].parent_id",
        ),
    ],
)
def test_invalid_identity_graph_names_the_offending_path(
    nodes: tuple[IterationSummary, ...], path: str
) -> None:
    with pytest.raises(ValidationError, match=path.replace("[", r"\[").replace("]", r"\]")):
        IterationTree(revision="revision", nodes=nodes)


def test_catalog_has_an_explicit_iteration_disposition_for_every_builtin() -> None:
    registrations = built_in_orchestrations().registrations()

    assert tuple(registration.plugin.id for registration in registrations) == tuple(
        registration.plugin.id for registration in catalog.BUILT_INS
    )
    assert all(
        registration.iteration_support is not IterationSupport.UNMAPPED
        for registration in registrations
    )
    assert all(
        registration.iteration_support is IterationSupport.PROJECTED
        for registration in registrations
    )

    projection = project_issue_queue_state(IssueQueueStrategyState(), experiment_revision=0)
    assert isinstance(projection.iterations, IterationTree)
    assert projection.iterations.nodes == (
        IterationSummary(
            id="board:issues",
            kind=IterationKind.BOARD,
            label="Issue board",
            ordinal=0,
            status=IterationStatus.PENDING,
        ),
    )


def test_issue_queue_projects_board_items_in_stable_identity_order() -> None:
    def issue(identifier: int, status: IssueStatus, *, attempts: int = 0) -> IssueRow:
        return IssueRow(
            id=identifier,
            type=IssueType.BUG,
            status=status,
            title=f"Issue {identifier}",
            attempts=attempts,
            created_by="operator",
            created_iter=1,
        )

    state = IssueQueueStrategyState(
        board=BoardState(
            bootstrap_done=True,
            issues=(
                issue(4, IssueStatus.BLOCKED, attempts=2),
                issue(1, IssueStatus.IN_PROGRESS, attempts=1),
                issue(3, IssueStatus.CLOSED),
                issue(2, IssueStatus.OPEN),
            ),
        )
    )

    projected = project_issue_queue_state(state, experiment_revision=7).iterations
    reopened = project_issue_queue_state(
        IssueQueueStrategyState.model_validate_json(state.model_dump_json()),
        experiment_revision=7,
    ).iterations

    assert isinstance(projected, IterationTree)
    assert reopened == projected
    assert [
        (node.id, node.parent_id, node.kind, node.ordinal, node.status) for node in projected.nodes
    ] == [
        ("board:issues", None, IterationKind.BOARD, 0, IterationStatus.ACTIVE),
        ("item:1", "board:issues", IterationKind.ITEM, 1, IterationStatus.ACTIVE),
        ("item:2", "board:issues", IterationKind.ITEM, 2, IterationStatus.PENDING),
        ("item:3", "board:issues", IterationKind.ITEM, 3, IterationStatus.COMPLETED),
        ("item:4", "board:issues", IterationKind.ITEM, 4, IterationStatus.FAILED),
    ]
    assert projected.nodes[0].child_count == 4
    assert projected.nodes[-1].headline_metrics[0].model_dump() == {
        "id": "attempts",
        "value": 2.0,
        "unit": None,
        "direction": None,
    }


def test_dynamic_iterations_group_attempts_under_stable_workstreams() -> None:
    parent = RevisionRef.of_git_commit("parent")
    attempts = (
        AttemptRecord(
            plan=WorkPlan(kind=WorkKind.IMPLEMENT, work_id="cache", title="Cache lookups"),
            sequence=3,
            attempt=AttemptId(root="attempt:cache:2"),
            parent=parent,
            phase=WorkPhase.DONE,
        ),
        AttemptRecord(
            plan=WorkPlan(kind=WorkKind.IMPLEMENT, work_id="parser", title="Parser"),
            sequence=2,
            attempt=AttemptId(root="attempt:parser:1"),
            parent=parent,
            phase=WorkPhase.REVIEW,
        ),
        AttemptRecord(
            plan=WorkPlan(kind=WorkKind.IMPLEMENT, work_id="cache", title="Cache lookups"),
            sequence=1,
            attempt=AttemptId(root="attempt:cache:1"),
            parent=parent,
            phase=WorkPhase.DONE,
        ),
    )

    tree = project_strategy_state(
        DynamicStrategyState(attempts=attempts), experiment_revision=9
    ).iterations

    assert isinstance(tree, IterationTree)
    roots = [node for node in tree.nodes if node.parent_id is None]
    assert [(node.id, node.ordinal, node.child_count) for node in roots] == [
        ("workstream:cache", 1, 2),
        ("workstream:parser", 2, 1),
    ]
    assert [
        (node.id, node.parent_id, node.kind, node.ordinal)
        for node in tree.nodes
        if node.parent_id is not None
    ] == [
        ("attempt:cache:1", "workstream:cache", IterationKind.ATTEMPT, 1),
        ("attempt:cache:2", "workstream:cache", IterationKind.ATTEMPT, 3),
        ("attempt:parser:1", "workstream:parser", IterationKind.ATTEMPT, 2),
    ]


def test_iteration_contract_rejects_unknown_keys_and_closed_set_values() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        IterationSummary.model_validate(
            {
                "id": "round:1",
                "kind": "round",
                "label": "Round 1",
                "ordinal": 1,
                "status": "completed",
                "child_count": 0,
                "headline_metrics": (),
                "surprise": True,
            },
            strict=True,
        )
    with pytest.raises(ValidationError, match="kind"):
        IterationSummary.model_validate(
            {
                "id": "round:1",
                "kind": "epoch",
                "label": "Round 1",
                "ordinal": 1,
                "status": "completed",
            },
            strict=True,
        )
