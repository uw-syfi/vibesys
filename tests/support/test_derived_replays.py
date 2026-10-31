"""A derived replay set is stored under a hash of everything it can derive from, never read stale."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.derived_replays import (
    CACHE_VARIABLE,
    KEEP,
    POLICY,
    RETAIN_SECONDS,
    cache_directory,
    find,
    input_files,
    inputs,
    key,
    main,
    materialize,
    selecting_inputs,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_POLICY = """
[[components]]
id = "clients"
root = "clients"

[[components]]
id = "derived_replays"
class = "manual"
job = "clients"
roots = ["schemas", "libs/core/src", { path = "libs/imported/src", selects = false }]
files = ["uv.lock", "libs/core/pyproject.toml", { path = "libs/imported/pyproject.toml", selects = false }]
"""
_FILES = {
    "schemas/api.proto": b"message A {}",
    "schemas/nested/store.proto": b"message S {}",
    "libs/core/src/core/values.py": b"X = 1",
    "libs/core/src/core/data/template.j2": b"{{ name }}",
    "libs/core/pyproject.toml": b"[project]",
    "libs/imported/src/imported/api.py": b"Y = 2",
    "libs/imported/pyproject.toml": b"[project]\nname = 'imported'",
    "uv.lock": b"version = 1",
}
_PYTHON = (3, 12, 3)


def _repository(root: Path) -> Path:
    for name, payload in _FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(payload)
    (root / POLICY).parent.mkdir(parents=True, exist_ok=True)
    (root / POLICY).write_text(_POLICY)
    return root


def _key(root: Path, python: tuple[int, ...] = _PYTHON) -> str:
    return key(root, python=python)


def _write_set(directory: Path) -> None:
    (directory / "shared").mkdir()
    (directory / "shared" / "run.pb").write_bytes(b"replay")


def _write_nothing(directory: Path) -> None:
    assert directory.is_dir()


def _same(value: str) -> Callable[[], str]:
    """The key of an input that does not change while a set is made."""
    return lambda: value


@given(
    name=st.sampled_from(sorted(_FILES)),
    payload=st.binary(max_size=16).filter(lambda value: value not in _FILES.values()),
)
def test_a_changed_byte_of_any_input_names_a_different_set(name: str, payload: bytes) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = _repository(Path(directory))
        before = _key(root)

        (root / name).write_bytes(payload)

        assert _key(root) != before


def test_every_file_under_a_root_is_an_input_whatever_its_kind(tmp_path: Path) -> None:
    """A data file, a constant and a renamed module are inputs as much as a function is."""
    root = _repository(tmp_path)
    before = _key(root)

    (root / "libs/core/src/core/data/template.j2").write_bytes(b"changed")
    template = _key(root)
    (root / "libs/core/src/core/constants.py").write_text("LIMIT = 3")
    added = _key(root)
    (root / "libs/core/src/core/constants.py").rename(root / "libs/core/src/core/limits.py")

    assert len({before, template, added, _key(root)}) == 4


def test_the_key_names_the_roots_and_files_and_nothing_beside_them(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = _key(root)

    (root / "elsewhere.py").write_text("unrelated")
    (root / "libs/core/tests").mkdir()
    (root / "libs/core/tests/test_values.py").write_text("a test of the library, not part of it")
    (root / "schemas/__pycache__").mkdir()
    (root / "schemas/__pycache__/api.cpython-312.pyc").write_bytes(b"compiled")
    (root / "libs/core/src/core.egg-info").mkdir()
    (root / "libs/core/src/core.egg-info/PKG-INFO").write_text("Version: 1")
    (root / "libs/core/src/core/.DS_Store").write_bytes(b"finder")

    assert _key(root) == before


def test_the_python_version_is_part_of_the_key(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    assert len({_key(root, (3, 12, 3)), _key(root, (3, 12, 4)), _key(root, (3, 13, 0))}) == 3


def test_a_dependency_bump_in_the_lock_file_names_a_different_set(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = _key(root)

    (root / "uv.lock").write_text('version = 1\n[[package]]\nname = "protobuf"\nversion = "7.0"\n')

    assert _key(root) != before


def test_the_key_does_not_depend_on_where_the_repository_is(tmp_path: Path) -> None:
    first = _repository(tmp_path / "first")
    second = _repository(tmp_path / "second")

    assert _key(first) == _key(second)


def test_a_missing_input_is_named(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "uv.lock").unlink()

    with pytest.raises(FileNotFoundError, match=r"uv\.lock.*derived_replays"):
        input_files(root)


@pytest.mark.parametrize("copies", [0, 2])
def test_the_policy_must_declare_the_component_exactly_once(tmp_path: Path, copies: int) -> None:
    root = _repository(tmp_path)
    component = "[[components]]" + _POLICY.split("[[components]]")[2]
    (root / POLICY).write_text(component * copies if copies else "")

    with pytest.raises(ValueError, match="exactly one component"):
        inputs(root)


def test_an_entry_that_does_not_select_still_keys_the_set(tmp_path: Path) -> None:
    """Import-only libraries regenerate the set, and a change to one selects no job."""
    root = _repository(tmp_path)
    before = _key(root)

    (root / "libs/imported/src/imported/api.py").write_text("Y = 3")
    source = _key(root)
    (root / "libs/imported/pyproject.toml").write_text("[project]\nname = 'renamed'")

    assert len({before, source, _key(root)}) == 3
    assert "libs/imported/src" in inputs(root)
    assert "libs/imported/src" not in selecting_inputs(root)
    assert "libs/imported/pyproject.toml" not in selecting_inputs(root)
    assert {"schemas", "libs/core/src", "uv.lock"} <= set(selecting_inputs(root))


def test_every_selection_root_is_a_key_root_in_a_synthetic_policy(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    assert set(selecting_inputs(root)) < set(inputs(root))


@pytest.mark.parametrize("entry", ["42", "{ selects = false }", "{ path = 3 }"])
def test_an_entry_that_is_neither_a_path_nor_a_table_with_one_is_rejected(
    tmp_path: Path, entry: str
) -> None:
    root = _repository(tmp_path)
    (root / POLICY).write_text('[[components]]\nid = "derived_replays"\nroots = [' + entry + "]\n")

    with pytest.raises(TypeError, match="derived_replays entry"):
        inputs(root)


def test_every_selection_root_of_the_real_policy_is_a_key_root() -> None:
    """The jobs a change selects are a subset of what keys the set, never the other way round."""
    key_inputs, selecting = set(inputs()), set(selecting_inputs())

    assert selecting <= key_inputs
    assert selecting, "the policy selects nothing from the derived_replays component"
    assert key_inputs - selecting, "the policy marks no import-only entry key-only"


def test_the_real_policy_selects_on_what_the_projection_runs_and_only_keys_what_it_imports() -> (
    None
):
    key_inputs, selecting = set(inputs()), set(selecting_inputs())
    ran = {
        "proto/vibesys",
        "libs/vs-core/src",
        "libs/vs-proto/src",
        "libs/vs-runlog/src",
        "libs/vs-decode/src",
        "libs/vs-redaction/src",
        "src/vibesys/records",
        "src/vibesys/search_spans",
        "uv.lock",
        "pyproject.toml",
        "tests/support/materialize_replays.py",
    }
    only_imported = {
        f"libs/{lib}/src"
        for lib in (
            "vs-agent",
            "vs-async-ops",
            "vs-evaluation",
            "vs-evaluator-protocol",
            "vs-faults",
            "vs-issue-tracker",
            "vs-mcp",
            "vs-project",
            "vs-prompts",
            "vs-runtime",
            "vs-sandbox",
            "vs-sim",
            "vs-slurm",
        )
    }

    assert ran <= selecting
    assert only_imported <= key_inputs
    assert not only_imported & selecting
    assert {"libs/vs-faults/src", "libs/vs-issue-tracker/src"} <= key_inputs


def test_the_real_policy_lists_what_the_generators_and_the_environment_need() -> None:
    """The list CI selects on also keys the set: the lock, the project files, the generators."""
    repository = Path(__file__).resolve().parents[2]
    names = set(inputs())
    files = {path.relative_to(repository).as_posix() for path in input_files()}

    assert {"uv.lock", "pyproject.toml", "tests/support/materialize_replays.py"} <= names
    assert {"tests/support/derived_replays.py", "src/vibesys/records/_spans.py"} <= files
    assert "libs/vs-core/src/vs_core/_corevalue.py" in files
    assert any(name.endswith(".proto") for name in files)
    assert len(key()) == 32


def test_a_set_is_generated_once_and_found_afterwards(tmp_path: Path) -> None:
    calls: list[Path] = []

    def produce(directory: Path) -> None:
        calls.append(directory)
        _write_set(directory)

    first = materialize(cache=tmp_path, current_key=_same("k1"), produce=produce)
    second = materialize(cache=tmp_path, current_key=_same("k1"), produce=produce)

    assert first == second == tmp_path / "k1"
    assert (first / "shared" / "run.pb").read_bytes() == b"replay"
    assert (first / "variants").is_dir()
    assert len(calls) == 1
    assert find(cache=tmp_path, set_key="k1") == first
    assert find(cache=tmp_path, set_key="k2") is None


def test_a_set_that_fails_midway_leaves_nothing_a_reader_could_take_for_complete(
    tmp_path: Path,
) -> None:
    def crash(directory: Path) -> None:
        _write_set(directory)
        message = "projection failed"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="projection failed"):
        materialize(cache=tmp_path, current_key=_same("k1"), produce=crash)

    assert list(tmp_path.iterdir()) == []
    assert find(cache=tmp_path, set_key="k1") is None
    published = materialize(cache=tmp_path, current_key=_same("k1"), produce=_write_set)
    assert published == tmp_path / "k1"


def test_a_generator_that_wrote_nothing_did_not_complete(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"wrote no \.pb file"):
        materialize(cache=tmp_path, current_key=_same("k1"), produce=_write_nothing)

    assert list(tmp_path.iterdir()) == []


def test_a_set_made_while_an_input_changed_is_not_published(tmp_path: Path) -> None:
    """The files it read may be from either version, and reverting the edit would revive its key."""
    keys = iter(["before", "after"])

    with pytest.raises(RuntimeError, match=r"changed while .*before became after"):
        materialize(cache=tmp_path, current_key=lambda: next(keys), produce=_write_set)

    assert list(tmp_path.iterdir()) == []


def test_two_processes_making_one_set_both_get_it(tmp_path: Path) -> None:
    """The second to finish finds the first one's set in place and discards its own."""

    def racing(directory: Path) -> None:
        def winner(other: Path) -> None:
            (other / "shared").mkdir()
            (other / "shared" / "run.pb").write_bytes(b"winner")

        materialize(cache=tmp_path, current_key=_same("k1"), produce=winner)
        _write_set(directory)

    result = materialize(cache=tmp_path, current_key=_same("k1"), produce=racing)

    assert (result / "shared" / "run.pb").read_bytes() == b"winner"
    assert [path.name for path in tmp_path.iterdir()] == ["k1"]


def test_a_changed_key_makes_a_new_set_and_keeps_the_old_one(tmp_path: Path) -> None:
    old = materialize(cache=tmp_path, current_key=_same("old"), produce=_write_set)
    new = materialize(cache=tmp_path, current_key=_same("new"), produce=_write_set)

    assert old != new
    assert old.is_dir()
    assert new.is_dir()


def _used(cache: Path, name: str, seconds_ago: int) -> None:
    """Make ``name`` last used ``seconds_ago`` before the cache was last written."""
    now = cache.stat().st_mtime
    os.utime(cache / name, (now - seconds_ago, now - seconds_ago))


@given(extra=st.integers(min_value=1, max_value=6))
def test_only_the_old_sets_beyond_the_most_recent_are_pruned(extra: int) -> None:
    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory)
        names = [f"old{index}" for index in range(KEEP + extra)]
        for name in names:
            materialize(cache=cache, current_key=_same(name), produce=_write_set)
        for age, name in enumerate(names):
            _used(cache, name, (len(names) - age) * RETAIN_SECONDS)

        materialize(cache=cache, current_key=_same("current"), produce=_write_set)
        kept = sorted(path.name for path in cache.iterdir())

    assert kept == sorted([*names[len(names) - (KEEP - 1) :], "current"])


def test_a_set_used_within_a_day_of_the_newest_is_never_pruned(tmp_path: Path) -> None:
    """A long run on an older checkout keeps the set it started with."""
    names = [f"set{index}" for index in range(KEEP + 3)]
    for name in names:
        materialize(cache=tmp_path, current_key=_same(name), produce=_write_set)
    for age, name in enumerate(names):
        _used(tmp_path, name, age * 60)

    materialize(cache=tmp_path, current_key=_same("current"), produce=_write_set)

    assert len(list(tmp_path.iterdir())) == KEEP + 4


def test_finding_a_set_counts_as_using_it(tmp_path: Path) -> None:
    names = [f"set{index}" for index in range(KEEP)]
    for name in names:
        materialize(cache=tmp_path, current_key=_same(name), produce=_write_set)
    for age, name in enumerate(names):
        _used(tmp_path, name, (len(names) - age) * RETAIN_SECONDS)

    assert find(cache=tmp_path, set_key="set0") is not None
    materialize(cache=tmp_path, current_key=_same("current"), produce=_write_set)

    assert (tmp_path / "set0").is_dir()
    assert not (tmp_path / "set1").exists()


def test_the_cache_is_shared_by_every_checkout_unless_told_otherwise(tmp_path: Path) -> None:
    assert cache_directory({CACHE_VARIABLE: str(tmp_path)}) == tmp_path
    assert cache_directory({"XDG_CACHE_HOME": str(tmp_path)}) == (
        tmp_path / "vibesys" / "derived-replays"
    )


def test_the_command_line_prints_the_key_for_a_ci_step_and_exits_1_for_a_missing_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(CACHE_VARIABLE, str(tmp_path))

    assert main(["key"]) == 0
    printed = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert re.fullmatch(r"[0-9a-f]{32}", printed["key"])
    assert printed["path"] == str(tmp_path)

    assert main(["path"]) == 1
    assert capsys.readouterr().out == ""

    materialize(cache=tmp_path, current_key=_same(printed["key"]), produce=_write_set)
    assert main(["path"]) == 0
    assert capsys.readouterr().out == f"{tmp_path / printed['key']}\n"
    assert main(["bogus"]) == 2
