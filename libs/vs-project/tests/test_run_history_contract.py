"""Contract of the ``RunHistory`` interface, run against every implementation.

Each case records the same generated runs (a manifest and other portable files per run)
in every implementation, then reads them back. ``FakeRunHistory`` holds them in memory.
``GitRunHistory`` holds them as a project does: each run's files committed on its own
run branch, over the in-memory Git fake (the git contract suite holds it to real Git,
``branches`` and ``tree_files`` included), with the project's own
branch checked out, with the last run's branch checked out, and with run branches
stacked the way VibeSys cut them before runs knew their home branch (each branch also
carrying stale copies of every earlier run's files). Every implementation must read
back exactly what was recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_project.api import (
    GitRunHistory,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    Project,
    ProjectStateError,
    RunEnvironmentRecord,
)
from vs_project.api.testing import FakeGitRepositories, FakeRunHistory, run_execution_record
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from vs_project.api import GitRepository, RunHistory

_START = datetime(2026, 1, 1, tzinfo=UTC)
_MANIFEST = "run.json"


@dataclass(frozen=True)
class RecordedRun:
    """One run as recorded: its id and every portable file, the manifest included."""

    run_id: str
    files: Mapping[str, bytes]


def _manifest(run_id: str, minute: int) -> bytes:
    manifest = OrchestrationRunManifest(
        schema_version=5,
        run_id=run_id,
        project_id="contract-project",
        input_fingerprint="0" * 64,
        display_name=f"Run {run_id}",
        branch=f"vibesys-runs/{run_id}",
        created_at=_START + timedelta(minutes=minute),
        vibesys_version="test",
        trusted_input_baseline="0" * 40,
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="plain", config_version=1, options={}),
    )
    return manifest.model_dump_json().encode()


# -- implementations ---------------------------------------------------------------------

type Build = Callable[[Path, Sequence[RecordedRun]], RunHistory]


def _fake(_root: Path, runs: Sequence[RecordedRun]) -> RunHistory:
    return FakeRunHistory({run.run_id: run.files for run in runs})


def _write(root: Path, run_id: str, files: Mapping[str, bytes]) -> None:
    directory = root / Project.open(root).state.run_directory(run_id)
    for relative, contents in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)


def _commit_all(repository: GitRepository, message: str) -> None:
    repository.stage_all(["."], force=True)
    repository.commit(message, allow_empty=True)


def _stale(files: Mapping[str, bytes]) -> dict[str, bytes]:
    """The same files with other contents: what an earlier cut of a branch held."""
    return {
        relative: (b"{}" if relative == _MANIFEST else b"stale " + contents)
        for relative, contents in files.items()
    }


def _git_project(
    open_repository: Callable[[Path], GitRepository],
    *,
    stacked: bool = False,
    stay_on_last: bool = False,
) -> Build:
    def build(root: Path, runs: Sequence[RecordedRun]) -> RunHistory:
        root.mkdir()
        repository = open_repository(root)
        repository.initialize(initial_branch="main")
        (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        _commit_all(repository, "baseline")
        for run in runs:
            if not stacked:
                repository.switch_branch("main")
            repository.switch_branch(f"vibesys-runs/{run.run_id}", create=True)
            if stacked:
                # Commit a stale copy first; the branch cut from this one carries it.
                _write(root, run.run_id, _stale(run.files))
                _commit_all(repository, f"vibesys: initialize run {run.run_id}")
            _write(root, run.run_id, run.files)
            _commit_all(repository, f"vibesys: record run {run.run_id}")
        if stacked:
            # Commit the stale copies on every later branch after each run's real files.
            for earlier, later in pairwise(runs):
                repository.switch_branch(f"vibesys-runs/{later.run_id}")
                _write(root, earlier.run_id, _stale(earlier.files))
                _commit_all(repository, f"vibesys: stale copy of {earlier.run_id}")
        if not (stay_on_last and runs):
            repository.switch_branch("main")
        return GitRunHistory(Project.open(root), open_repository(root))

    return build


def _in_memory_git(**options: bool) -> Build:
    """A build over a fresh in-memory Git disk per call (one disk per test world)."""

    def build(root: Path, runs: Sequence[RecordedRun]) -> RunHistory:
        return _git_project(FakeGitRepositories(), **options)(root, runs)

    return build


IMPLEMENTATIONS: dict[str, Build] = {
    "fake": _fake,
    "git": _in_memory_git(),
    "git-last-run-checked-out": _in_memory_git(stay_on_last=True),
    "git-stacked": _in_memory_git(stacked=True),
}

EACH = pytest.mark.parametrize("implementation", sorted(IMPLEMENTATIONS))

# -- generated runs ----------------------------------------------------------------------

# Run ids as VibeSys generates them: each also names a valid branch.
_RUN_IDS = st.from_regex(r"[a-z0-9][a-z0-9-]{0,11}", fullmatch=True)
_NAMES = st.from_regex(r"[a-z][a-z0-9_-]{0,7}(\.json|\.md)?", fullmatch=True)
_RELATIVE = (
    st.lists(_NAMES, min_size=1, max_size=3)
    .map("/".join)
    .filter(lambda path: path != _MANIFEST and not path.startswith(f"{_MANIFEST}/"))
)
_DOCUMENTS = st.dictionaries(_RELATIVE, st.binary(max_size=24), max_size=3).filter(
    lambda documents: (
        not any(other.startswith(f"{path}/") for path in documents for other in documents)
    )
)


@st.composite
def _runs(draw: st.DrawFn) -> list[RecordedRun]:
    run_ids = draw(st.lists(_RUN_IDS, min_size=1, max_size=3, unique=True))
    return [
        RecordedRun(
            run_id=run_id,
            files={
                _MANIFEST: _manifest(run_id, draw(st.integers(min_value=0, max_value=3))),
                **draw(_DOCUMENTS),
            },
        )
        for run_id in run_ids
    ]


def _expected_order(runs: Sequence[RecordedRun]) -> list[str]:
    def key(run: RecordedRun) -> tuple[datetime, str]:
        created = OrchestrationRunManifest.model_validate_json(run.files[_MANIFEST]).created_at
        return created, run.run_id

    return [run.run_id for run in sorted(runs, key=key)]


_SETTINGS = settings(
    max_examples=examples(pr=4, nightly=40),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@EACH
@_SETTINGS
@given(runs=_runs())
def test_reads_back_every_recorded_run_oldest_first(
    implementation: str, tmp_path_factory: pytest.TempPathFactory, runs: list[RecordedRun]
) -> None:
    history = IMPLEMENTATIONS[implementation](tmp_path_factory.mktemp("h") / "p", runs)

    recorded = history.recorded()

    assert [manifest.run_id for manifest in recorded.runs] == _expected_order(runs)
    assert recorded.unreadable == ()
    for run in runs:
        assert history.manifest(run.run_id).model_dump_json().encode() == run.files[_MANIFEST]
        for relative, contents in run.files.items():
            assert history.document(run.run_id, relative) == contents
        assert {
            item.relative_path.as_posix(): item.contents for item in history.documents(run.run_id)
        } == dict(run.files)
        paths = [item.relative_path for item in history.documents(run.run_id)]
        assert paths == sorted(paths, key=PurePosixPath.as_posix)


@EACH
@_SETTINGS
@given(runs=_runs())
def test_reads_change_nothing_a_later_read_sees(
    implementation: str, tmp_path_factory: pytest.TempPathFactory, runs: list[RecordedRun]
) -> None:
    history = IMPLEMENTATIONS[implementation](tmp_path_factory.mktemp("h") / "p", runs)
    first = history.recorded()
    for run in runs:
        history.documents(run.run_id)
        history.manifest(run.run_id)

    assert history.recorded() == first


@EACH
@_SETTINGS
@given(runs=_runs(), data=st.data())
def test_a_scan_delivers_each_run_once_and_a_named_read_only_what_was_asked(
    implementation: str,
    tmp_path_factory: pytest.TempPathFactory,
    runs: list[RecordedRun],
    data: st.DataObject,
) -> None:
    history = IMPLEMENTATIONS[implementation](tmp_path_factory.mktemp("h") / "p", runs)
    held = [run.run_id for run in runs]
    asked = data.draw(st.lists(st.sampled_from([*held, "absent", "Upper", "a/b", "-x", ""])))

    named = history.named(asked)

    assert sorted(manifest.run_id for manifest in history.scan()) == sorted(held)
    assert sorted(manifest.run_id for manifest in named.runs) == sorted(set(asked) & set(held))
    assert named.unreadable == ()
    assert [manifest.run_id for manifest in named.runs] == [
        run_id for run_id in _expected_order(runs) if run_id in asked
    ]


@EACH
def test_a_run_never_recorded_has_no_manifest_and_no_files(
    implementation: str, tmp_path: Path
) -> None:
    run = RecordedRun(run_id="known", files={_MANIFEST: _manifest("known", 0)})
    history = IMPLEMENTATIONS[implementation](tmp_path / "p", [run])

    with pytest.raises(ProjectStateError):
        history.manifest("unknown")
    assert history.document("unknown", _MANIFEST) is None
    assert history.documents("unknown") == ()
    assert history.document("known", "absent.md") is None


@EACH
@pytest.mark.parametrize("run_id", ["", "Upper", "../escape", "a/b", "-x"])
def test_an_invalid_run_id_is_refused(implementation: str, tmp_path: Path, run_id: str) -> None:
    history = IMPLEMENTATIONS[implementation](tmp_path / "p", [])

    with pytest.raises(ProjectStateError):
        history.manifest(run_id)
    with pytest.raises(ProjectStateError):
        history.document(run_id, _MANIFEST)
    with pytest.raises(ProjectStateError):
        history.documents(run_id)


@EACH
@pytest.mark.parametrize("path", ["", "../run.json", "/abs", "a//b", "a\\b"])
def test_a_path_outside_the_run_directory_is_refused(
    implementation: str, tmp_path: Path, path: str
) -> None:
    run = RecordedRun(run_id="known", files={_MANIFEST: _manifest("known", 0)})
    history = IMPLEMENTATIONS[implementation](tmp_path / "p", [run])

    with pytest.raises(ProjectStateError):
        history.document("known", path)


@EACH
@pytest.mark.parametrize("damage", [b"{", b"[]", b'{"schema_version": 4}', b"{}"])
def test_an_unreadable_manifest_is_reported_and_others_still_read(
    implementation: str, tmp_path: Path, damage: bytes
) -> None:
    runs = [
        RecordedRun(run_id="good", files={_MANIFEST: _manifest("good", 0)}),
        RecordedRun(run_id="damaged", files={_MANIFEST: damage}),
    ]
    history = IMPLEMENTATIONS[implementation](tmp_path / "p", runs)

    recorded = history.recorded()

    assert [manifest.run_id for manifest in recorded.runs] == ["good"]
    assert [run_id for run_id, _error in recorded.unreadable] == ["damaged"]
    with pytest.raises(ProjectStateError):
        history.manifest("damaged")
    assert [manifest.run_id for manifest in history.scan()] == ["good"]
    named = history.named(["damaged", "good"])
    assert [manifest.run_id for manifest in named.runs] == ["good"]
    assert [run_id for run_id, _error in named.unreadable] == ["damaged"]
