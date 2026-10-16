"""The handoff owner writes each planning call's file into the run's state, where it stays.

The owner renders a `WriteHandoff` from the dynamic templates and writes it into the run's
portable state through `Project`. These tests drive the owner over a real project on disk: the
file sits at the path the owner names (relative to the project), it says what the planner's reply
said, a reopened project still holds it, and `observe` says the write landed only once it has.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.dynamic_handoff import HANDOFF_NAMESPACE, HandoffOwner, render_handoff
from vibesys.orchestration.dynamic.search.api import (
    Expectation,
    HandoffNote,
    HandoffWritten,
    WorkKind,
    WorkPlan,
    WriteHandoff,
)
from vibesys.run.project_policy import build_project_path_policy
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_runtime.api.core import Applied, NotApplied
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    import pytest

RUN = "run-1"

_text = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9 .,:-]{0,30}", fullmatch=True)
_ids = st.from_regex(r"h[0-9]{1,3}", fullmatch=True)


def _project(root: Path) -> Project:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    project = Project.open(root)
    project.state.create_project("test", now=datetime(2026, 1, 1, tzinfo=UTC))
    run = project.state.new_run_manifest(
        "Run 1",
        run_id=RUN,
        trusted_input_baseline="a" * 40,
        branch=f"vibesys/{RUN}",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(run)
    return project


def _owner(project: Project) -> HandoffOwner:
    return HandoffOwner(
        project.state.agent_visible_namespace(RUN, HANDOFF_NAMESPACE),
        earlier=project.state.portable_namespace(RUN, HANDOFF_NAMESPACE),
    )


@st.composite
def _requests(draw: st.DrawFn) -> WriteHandoff:
    ids = draw(st.lists(_ids, max_size=3, unique=True))
    profile = draw(st.booleans())
    scheduled = [
        WorkPlan(
            kind=WorkKind.IMPLEMENT,
            work_id=identifier,
            title=draw(_text),
            hypothesis=draw(_text),
            task=draw(_text),
            pass_criteria=draw(_text),
        )
        for identifier in ids
    ]
    if profile:
        scheduled.append(
            WorkPlan(
                kind=WorkKind.PROFILE,
                work_id="p1",
                question=draw(_text),
                decision_impact=draw(_text),
            )
        )
    expectations = tuple(
        Expectation(
            work_id=item.work_id,
            expected=draw(_text),
            if_it_succeeds=draw(_text),
            if_it_fails=draw(_text),
        )
        for item in scheduled
    )
    return WriteHandoff(
        call=draw(st.integers(1, 9999)),
        reasoning=draw(_text),
        scheduled=tuple(scheduled),
        handoff=HandoffNote(
            expectations=expectations, doubts=tuple(draw(st.lists(_text, max_size=3)))
        ),
    )


def _said(request: WriteHandoff) -> list[str]:
    """Every piece of the planner's reply the file must carry."""
    texts = [request.reasoning, *request.handoff.doubts]
    for item in request.scheduled:
        texts += [item.work_id, item.title, item.hypothesis, item.task, item.pass_criteria]
        texts += [item.question, item.decision_impact]
    for expected in request.handoff.expectations:
        texts += [expected.expected, expected.if_it_succeeds, expected.if_it_fails]
    return [text for text in texts if text]


@settings(max_examples=examples(pr=15, nightly=100), deadline=None)
@given(request=_requests())
def test_the_file_carries_the_reply_at_the_named_path_and_survives_reopening(
    tmp_path_factory: pytest.TempPathFactory, request: WriteHandoff
) -> None:
    root = tmp_path_factory.mktemp("handoff") / "project"
    project = _project(root)
    owner = _owner(project)
    assert isinstance(owner.observe(request), NotApplied)

    written = owner.perform(request)

    assert isinstance(written, HandoffWritten)
    assert written.path == (
        f".vibesys/state/agent-visible/{RUN}/{HANDOFF_NAMESPACE}/calls/call-{request.call:04d}.md"
    )
    text = (root / written.path).read_text(encoding="utf-8")
    assert text == render_handoff(request)
    assert all(piece in text for piece in _said(request))
    # A reopened project (a restart or a resume) holds the same file, and a replayed write is
    # recognised as already applied, then rewrites the same bytes.
    reopened = _owner(Project.open(root))
    assert reopened.observe(request) == Applied(written)
    assert reopened.perform(request) == written
    assert (root / written.path).read_text(encoding="utf-8") == text


@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
@given(calls=st.lists(st.integers(1, 9999), min_size=1, max_size=5, unique=True), base=_requests())
def test_every_write_lists_every_file_the_run_holds_oldest_call_first(
    tmp_path_factory: pytest.TempPathFactory, calls: list[int], base: WriteHandoff
) -> None:
    """A file whose own write was reported failed after its bytes landed is listed by the next."""
    root = tmp_path_factory.mktemp("handoff") / "project"
    owner = _owner(_project(root))
    for call in calls:
        written = owner.perform(base.model_copy(update={"call": call}))
        assert written.listed[-1] == written.path or written.path in written.listed
    paths = [
        f".vibesys/state/agent-visible/{RUN}/{HANDOFF_NAMESPACE}/calls/call-{call:04d}.md"
        for call in sorted(calls)
    ]
    assert list(written.listed) == paths
    assert all((root / path).is_file() for path in paths)


def test_the_planner_sees_the_file_through_its_confinement_and_cannot_change_it(
    tmp_path: Path,
) -> None:
    """The project's path policy for agents leaves the handoff files readable and read-only.

    The planner works in the run's project checkout; its confinement hides the run's record and
    local state and makes the framework state read-only. The file must be in neither hidden part.
    """
    root = tmp_path / "project"
    project = _project(root)
    request = WriteHandoff(call=1, reasoning="why", scheduled=(), handoff=HandoffNote())
    written = _owner(project).perform(request)
    (root / ".vibesys/state/local").mkdir()
    policy = build_project_path_policy(root, evaluator_source=None)
    assert {Path(".vibesys/state/runs"), Path(".vibesys/state/local")} <= set(policy.hidden_paths)
    path = PurePosixPath(written.path)
    assert not any(path.is_relative_to(hidden.as_posix()) for hidden in policy.hidden_paths)
    assert any(path.is_relative_to(guarded.as_posix()) for guarded in policy.read_only_paths)


@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
@given(request=_requests(), other=_text)
def test_a_file_that_differs_from_the_request_is_not_applied(
    tmp_path_factory: pytest.TempPathFactory, request: WriteHandoff, other: str
) -> None:
    project = _project(tmp_path_factory.mktemp("handoff") / "project")
    owner = _owner(project)
    owner.perform(request.model_copy(update={"reasoning": f"{request.reasoning} {other}"}))
    assert isinstance(owner.observe(request), NotApplied)


def test_files_an_earlier_run_kept_in_its_record_are_listed_where_the_planner_reads(
    tmp_path: Path,
) -> None:
    """A resumed run's handoff files move out of the hidden record without losing any."""
    root = tmp_path / "project"
    project = _project(root)
    earlier = project.state.portable_namespace(RUN, HANDOFF_NAMESPACE)
    old = WriteHandoff(
        call=1, reasoning="written before the move", scheduled=(), handoff=HandoffNote()
    )
    earlier.write_bytes("calls/call-0001.md", render_handoff(old).encode("utf-8"))

    owner = _owner(project)
    assert owner.observe(old) == Applied(
        HandoffWritten(path=_path(1), listed=(_path(1),)),
    )
    written = owner.perform(old.model_copy(update={"call": 2}))

    assert written.listed == (_path(1), _path(2))
    assert (root / _path(1)).read_text(encoding="utf-8") == render_handoff(old)
    assert earlier.read_bytes("calls/call-0001.md") is not None


def _path(call: int) -> str:
    return f".vibesys/state/agent-visible/{RUN}/{HANDOFF_NAMESPACE}/calls/call-{call:04d}.md"
