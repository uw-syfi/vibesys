"""A derived replay set is stored under the hash of its inputs, so a stale one is never read."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.derived_replays import (
    CACHE_VARIABLE,
    KEEP,
    cache_directory,
    covered,
    find,
    input_files,
    key,
    main,
    materialize,
)

INPUTS = ("schemas", "projector/records.py", "declarations")
_FILES = {
    "schemas/api.proto": b"message A {}",
    "schemas/nested/store.proto": b"message S {}",
    "projector/records.py": b"def project(): ...",
    "declarations/run.json": b"{}",
}


def _repository(root: Path) -> Path:
    for name, payload in _FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(payload)
    return root


def _write_set(directory: Path) -> None:
    (directory / "shared").mkdir()
    (directory / "shared" / "run.pb").write_bytes(b"replay")


@given(
    name=st.sampled_from(sorted(_FILES)),
    payload=st.binary(max_size=16).filter(lambda value: value not in _FILES.values()),
)
def test_a_changed_input_byte_names_a_different_set(name: str, payload: bytes) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = _repository(Path(directory))
        before = key(root, INPUTS)

        (root / name).write_bytes(payload)

        assert key(root, INPUTS) != before


def test_the_key_names_the_inputs_and_nothing_beside_them(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = key(root, INPUTS)

    (root / "elsewhere.py").write_text("unrelated")
    (root / "schemas" / "notes.md").write_text("not a schema")
    (root / "schemas" / "__pycache__").mkdir()
    (root / "schemas" / "__pycache__" / "api.py").write_text("compiled")

    assert key(root, INPUTS) == before


def test_a_file_added_under_an_input_directory_or_renamed_changes_the_key(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = key(root, INPUTS)

    (root / "schemas" / "extra.proto").write_bytes(b"message E {}")
    added = key(root, INPUTS)
    (root / "schemas" / "extra.proto").rename(root / "schemas" / "renamed.proto")

    assert len({before, added, key(root, INPUTS)}) == 3


def test_the_key_does_not_depend_on_where_the_repository_is(tmp_path: Path) -> None:
    first = _repository(tmp_path / "first")
    second = _repository(tmp_path / "second")

    assert key(first, INPUTS) == key(second, INPUTS)


def test_a_missing_input_is_named(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "projector" / "records.py").unlink()

    with pytest.raises(FileNotFoundError, match=r"projector/records\.py"):
        input_files(root, INPUTS)


def test_covered_agrees_with_the_files_the_key_reads(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "elsewhere.py").write_text("unrelated")
    (root / "schemas" / "notes.md").write_text("not a schema")

    read = {path.relative_to(root) for path in input_files(root, INPUTS)}
    every = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}

    assert {path for path in every if covered(root / path, root, INPUTS)} == read


def test_a_set_is_generated_once_and_found_afterwards(tmp_path: Path) -> None:
    calls: list[Path] = []

    def produce(directory: Path) -> None:
        calls.append(directory)
        _write_set(directory)

    first = materialize(cache=tmp_path, set_key="k1", produce=produce)
    second = materialize(cache=tmp_path, set_key="k1", produce=produce)

    assert first == second == tmp_path / "k1"
    assert (first / "shared" / "run.pb").read_bytes() == b"replay"
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
        materialize(cache=tmp_path, set_key="k1", produce=crash)

    assert list(tmp_path.iterdir()) == []
    assert find(cache=tmp_path, set_key="k1") is None
    assert materialize(cache=tmp_path, set_key="k1", produce=_write_set) == tmp_path / "k1"


def _write_nothing(directory: Path) -> None:
    assert directory.is_dir()


def test_a_generator_that_wrote_nothing_did_not_complete(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"wrote no \.pb file"):
        materialize(cache=tmp_path, set_key="k1", produce=_write_nothing)

    assert list(tmp_path.iterdir()) == []


def test_two_processes_making_one_set_both_get_it(tmp_path: Path) -> None:
    """The second to finish finds the first one's set in place and discards its own."""

    def racing(directory: Path) -> None:
        def winner(other: Path) -> None:
            (other / "shared").mkdir()
            (other / "shared" / "run.pb").write_bytes(b"winner")

        materialize(cache=tmp_path, set_key="k1", produce=winner)
        _write_set(directory)

    result = materialize(cache=tmp_path, set_key="k1", produce=racing)

    assert (result / "shared" / "run.pb").read_bytes() == b"winner"
    assert [path.name for path in tmp_path.iterdir()] == ["k1"]


def test_a_changed_key_makes_a_new_set_and_keeps_the_old_one(tmp_path: Path) -> None:
    old = materialize(cache=tmp_path, set_key="old", produce=_write_set)
    new = materialize(cache=tmp_path, set_key="new", produce=_write_set)

    assert old != new
    assert old.is_dir()
    assert new.is_dir()


@given(extra=st.integers(min_value=1, max_value=6))
def test_only_the_sets_used_most_recently_are_kept(extra: int) -> None:
    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory)
        names = [f"old{index}" for index in range(KEEP + extra)]
        for age, name in enumerate(names):
            materialize(cache=cache, set_key=name, produce=_write_set)
            os.utime(cache / name, (age, age))

        materialize(cache=cache, set_key="current", produce=_write_set)

        kept = sorted(path.name for path in cache.iterdir())
    newest = names[len(names) - (KEEP - 1) :]
    assert kept == sorted([*newest, "current"])


def test_finding_a_set_counts_as_using_it(tmp_path: Path) -> None:
    for age, name in enumerate(f"set{index}" for index in range(KEEP)):
        materialize(cache=tmp_path, set_key=name, produce=_write_set)
        os.utime(tmp_path / name, (age, age))

    assert find(cache=tmp_path, set_key="set0") is not None
    materialize(cache=tmp_path, set_key="current", produce=_write_set)

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

    materialize(cache=tmp_path, set_key=printed["key"], produce=_write_set)
    assert main(["path"]) == 0
    assert capsys.readouterr().out == f"{tmp_path / printed['key']}\n"
    assert main(["bogus"]) == 2
