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
    IterationUnavailable,
    IterationUnavailableReason,
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
from vibesys.orchestration.issue_queue.strategy.api import IssueQueueStrategyState
from vs_core.api import AttemptId, RevisionRef


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
    assert catalog.ISSUE_QUEUE.iteration_support is IterationSupport.EXTERNAL_STATE_UNAVAILABLE
    assert all(
        registration.iteration_support is IterationSupport.PROJECTED
        for registration in registrations
        if registration is not catalog.ISSUE_QUEUE
    )

    projection = project_issue_queue_state(IssueQueueStrategyState(), experiment_revision=0)
    assert isinstance(projection.iterations, IterationUnavailable)
    assert projection.iterations.reason is IterationUnavailableReason.EXTERNAL_STATE_UNAVAILABLE


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
