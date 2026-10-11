"""The exclusive root keeps what the user had there before the run.

The root can be the user's own checkout. Every grant restores it to the plan's base plus the
run's artifact directory and removes what earlier attempts left, but a file that was already
untracked or ignored before the first grant (a `.venv`, a data directory, scratch notes) is
the user's and is never deleted or rewritten. The first grant records those paths in the
receipt store before its first destructive step, and every later grant, after any host death,
reads that record instead of scanning a tree the attempts have since changed.

The host dies at any durable write or restore step, exactly as in
`test_exclusive_root_attempts`, whose harness this module reuses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.workspace_world import WorkspaceEnv, open_workspace_env

from vs_core.api import DiscardWorkspace, ObservationStatus, WorkspaceMode, WorkspacePlan
from vs_runtime.api.core import revision_ref

from .test_exclusive_root_attempts import (
    _ARTIFACT_DIR,
    _LEFTOVERS,
    _NEVER,
    _RESTORE_POINTS,
    _WRITES_PER_REQUEST,
    CrashingGit,
    Host,
    RestoreCrash,
    _ensure,
    _write_files,
)
from .test_workspace_requests import _attempt, _common, _status, _visible_files

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import GitRepository

_ROOT = WorkspaceMode.EXCLUSIVE_ROOT
_IGNORE_RULES = "*.cache\nbuild/\n.venv/\ndata/\n"
_PRE_IGNORED = (
    ".venv/bin/python",
    ".venv/lib/site/pkg.py",
    "data/train/part-0.bin",
    "data/index.bin",
    "models/weights.cache",
)
_PRE_UNTRACKED = ("notes.txt", "local/todo.md", "local/deep/draft.txt")
_PRE_RUN = (*_PRE_IGNORED, *_PRE_UNTRACKED)
"""Files that exist before the run: ignored ones and untracked ones."""


def _pre_content(name: str) -> str:
    return f"pre-run {name}\n"


def _write_pre_run(root: Path, names: tuple[str, ...]) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_pre_content(name), encoding="utf-8")


@dataclass(frozen=True)
class Step:
    """What one attempt does, and where the host dies while it is granted and ended."""

    ensure_dies_at: int
    discard_dies_at: int
    restore_dies_at: int
    creates: tuple[str, ...]
    modifies: tuple[str, ...]


_STEP = st.builds(
    Step,
    ensure_dies_at=st.sampled_from([_NEVER, *range(_WRITES_PER_REQUEST)]),
    discard_dies_at=st.sampled_from([_NEVER, *range(_WRITES_PER_REQUEST - 1)]),
    restore_dies_at=st.sampled_from([_NEVER, *range(_RESTORE_POINTS)]),
    creates=st.lists(st.sampled_from(_LEFTOVERS), unique=True).map(tuple),
    modifies=st.lists(st.sampled_from(_PRE_RUN), unique=True).map(tuple),
)


async def _play_steps(
    env: WorkspaceEnv, present: tuple[str, ...], steps: list[Step], crash: RestoreCrash
) -> None:
    host = Host(env, crash)
    root = env.hosts[-1].root
    (root.path / ".gitignore").write_text(_IGNORE_RULES, encoding="utf-8")
    await root.snapshot("ignore rules")
    base = revision_ref(cast("str", root.revision))
    plan = WorkspacePlan(mode=_ROOT, base=base)
    base_tree = {
        name: text
        for name, text in _visible_files(root.path).items()
        if not name.startswith(f"{_ARTIFACT_DIR}/")
    }
    _write_pre_run(root.path, present)
    # What the user's files must hold at the next grant: untouched, unless an attempt
    # modified them, in which case the attempt's bytes (nothing restores or deletes them).
    expected = {name: _pre_content(name) for name in present}
    for index, step in enumerate(steps):
        attempt = _attempt(f"a{index}")
        granted = await host.run(
            _ensure(attempt, f"ensure-{index}", plan),
            dies_at=step.ensure_dies_at,
            restore_dies_at=step.restore_dies_at,
        )
        assert _status(granted) is ObservationStatus.SUCCEEDED
        tree = {
            name: text
            for name, text in _visible_files(root.path).items()
            if not name.startswith(f"{_ARTIFACT_DIR}/")
        }
        # Base, plus the user's files byte for byte, and nothing an attempt created.
        assert tree == {**expected, **base_tree}, (index, step)
        for name in step.creates:
            assert not (root.path / name).exists()
        _write_files(root.path, step.creates)
        for name in step.modifies:
            if name in expected:
                expected[name] = f"modified by a{index}\n"
                (root.path / name).write_text(expected[name], encoding="utf-8")
        discard = DiscardWorkspace(**_common(attempt, f"discard-{index}"), attempt=attempt)
        released = await host.run(discard, dies_at=step.discard_dies_at)
        assert _status(released) is ObservationStatus.SUCCEEDED
    # One more grant after the last attempt ends shows the final cleanup too.
    last = _attempt("final")
    final = await host.run(_ensure(last, "ensure-final", plan))
    assert _status(final) is ObservationStatus.SUCCEEDED
    tree = {
        name: text
        for name, text in _visible_files(root.path).items()
        if not name.startswith(f"{_ARTIFACT_DIR}/")
    }
    assert tree == {**expected, **base_tree}


def _play(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    present: tuple[str, ...],
    steps: list[Step],
) -> None:
    path = tmp_path_factory.mktemp("baseline")
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(path / "operator-state"))
    crash = RestoreCrash()
    with open_workspace_env(
        path,
        memory_paths=(_ARTIFACT_DIR,),
        wrap_git=lambda git: cast("GitRepository", CrashingGit(git, crash)),
    ) as env:
        asyncio.run(_play_steps(env, present, steps, crash))
        for workspaces in env.hosts:
            asyncio.run(workspaces.close())


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    present=st.lists(st.sampled_from(_PRE_RUN), unique=True).map(tuple),
    steps=st.lists(_STEP, min_size=1, max_size=4),
)
def test_files_present_before_the_run_survive_every_grant_and_attempt_files_do_not(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    present: tuple[str, ...],
    steps: list[Step],
) -> None:
    _play(tmp_path_factory, monkeypatch, present, steps)


def test_a_venv_present_before_the_run_survives_two_root_attempts(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = Step(_NEVER, _NEVER, _NEVER, creates=_LEFTOVERS, modifies=())
    _play(tmp_path_factory, monkeypatch, _PRE_IGNORED, [step, step])


@pytest.mark.parametrize("dies_at", range(_WRITES_PER_REQUEST))
def test_a_host_that_dies_at_any_write_of_the_first_grant_loses_no_pre_run_file(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, dies_at: int
) -> None:
    first = Step(dies_at, dies_at, _NEVER, creates=_LEFTOVERS, modifies=())
    later = Step(_NEVER, _NEVER, _NEVER, creates=_LEFTOVERS, modifies=())
    _play(tmp_path_factory, monkeypatch, _PRE_RUN, [first, later])


@pytest.mark.parametrize("restore_dies_at", range(_RESTORE_POINTS))
def test_a_host_that_dies_in_the_first_restore_loses_no_pre_run_file(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    restore_dies_at: int,
) -> None:
    first = Step(_NEVER, _NEVER, restore_dies_at, creates=_LEFTOVERS, modifies=())
    later = Step(_NEVER, _NEVER, restore_dies_at, creates=_LEFTOVERS, modifies=())
    _play(tmp_path_factory, monkeypatch, _PRE_RUN, [first, later])
