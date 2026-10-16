"""Public iteration projection invariants and built-in catalog coverage."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
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
from vibesys.candidates.api import NotConfigured
from vibesys.orchestration.dynamic.core_policy.api import project_run_snapshot
from vibesys.orchestration.dynamic.search.api import (
    FinishedWork,
    HypothesisRecord,
    RoundRecord,
    Running,
    RunSnapshot,
    WorkKind,
    WorkPlan,
)
from vibesys.orchestration.issue_queue.core_policy.api import (
    project_run_snapshot as project_issue_queue_snapshot,
)
from vibesys.orchestration.issue_queue.search.api import IssueBrief, QueueSnapshot
from vs_core.api import AttemptId, RevisionRef
from vs_sim.api.testing import examples


def _node(identifier: str, ordinal: int) -> IterationSummary:
    return IterationSummary(
        id=identifier,
        kind=IterationKind.ROUND,
        label=identifier,
        ordinal=ordinal,
        status=IterationStatus.COMPLETED,
    )


@settings(max_examples=examples(pr=8, nightly=100))
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

    projection = project_issue_queue_snapshot(
        QueueSnapshot(round_idx=0, cursor="drain"), experiment_revision=0
    )
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
    def issue(identifier: int, status: str, *, attempts: int = 0) -> IssueBrief:
        return IssueBrief(
            id=identifier,
            type="bug",
            status=status,
            title=f"Issue {identifier}",
            attempts=attempts,
        )

    state = QueueSnapshot(
        round_idx=1,
        cursor="drain",
        issues=(
            issue(4, "blocked", attempts=2),
            issue(1, "in_progress", attempts=1),
            issue(3, "closed"),
            issue(2, "open"),
        ),
    )

    projected = project_issue_queue_snapshot(state, experiment_revision=7).iterations
    reopened = project_issue_queue_snapshot(
        QueueSnapshot.model_validate_json(state.model_dump_json()), experiment_revision=7
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
    cache = WorkPlan(kind=WorkKind.IMPLEMENT, work_id="cache", title="Cache lookups")
    parser = WorkPlan(kind=WorkKind.IMPLEMENT, work_id="parser", title="Parser")
    snapshot = RunSnapshot(
        baseline=NotConfigured(),
        hypotheses=(
            HypothesisRecord(
                hypothesis_id="cache",
                title="Cache lookups",
                hypothesis="caching helps",
                first_sequence=1,
                rounds=(RoundRecord(sequence=1, attempt=AttemptId(root="attempt:cache:1")),),
            ),
        ),
        # Finished out of the order they were scheduled in; the tree keeps schedule order.
        finished=(FinishedWork(sequence=1, plan=cache, parent=parent),),
        running=(Running(sequence=2, plan=parser),),
    )

    tree = project_run_snapshot(snapshot, experiment_revision=9).iterations

    assert isinstance(tree, IterationTree)
    assert [
        (node.id, node.parent_id, node.kind, node.ordinal, node.status, node.child_count)
        for node in tree.nodes
    ] == [
        (
            "workstream:000001.cache",
            None,
            IterationKind.WORKSTREAM,
            1,
            IterationStatus.COMPLETED,
            1,
        ),
        ("workstream:000002.parser", None, IterationKind.WORKSTREAM, 2, IterationStatus.ACTIVE, 0),
        (
            "attempt:cache:1",
            "workstream:000001.cache",
            IterationKind.ATTEMPT,
            1,
            IterationStatus.COMPLETED,
            0,
        ),
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
