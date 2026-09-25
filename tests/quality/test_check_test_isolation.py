"""Behavioral tests for the test isolation ratchet.

Every case builds a throwaway repository and drives the checker through its
public entry points (`main` and `measure`), so the assertions cover the
contract CI depends on: the exit code, the counts, and the files named in the
output.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import textwrap
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scripts.check_test_isolation import (
    EXIT_OK,
    EXIT_TOOL_ERROR,
    EXIT_VIOLATIONS,
    main,
    measure,
)

BASELINE = "test_isolation_baseline.jsonl"
CONFIG = (
    "[tool.vibesys.test_isolation]\n"
    'roots = ["tests", "libs/*/tests", "sdk/*/tests"]\n'
    f'baseline = "{BASELINE}"\n'
)
PATCH_SITE = 'monkeypatch.setattr("os.getcwd", None)'
SLEEP_SITE = "time.sleep(1)"
CHEAP = settings(deadline=None, max_examples=40)


def make_repo(root: Path, files: dict[str, str]) -> None:
    """Write a fixture `pyproject.toml` and the given files under ``root``."""
    (root / "pyproject.toml").write_text(CONFIG, encoding="utf-8")
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")


def write_baseline(root: Path, entries: list[tuple[str, str, int]]) -> None:
    """Write a JSONL baseline of (path, rule, count) entries."""
    lines = [json.dumps({"path": p, "rule": r, "count": c}) for p, r, c in entries]
    (root / BASELINE).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def read_baseline(root: Path) -> dict[tuple[str, str], int]:
    """Read the JSONL baseline back as a (path, rule) -> count map."""
    entries = [
        json.loads(line) for line in (root / BASELINE).read_text(encoding="utf-8").splitlines()
    ]
    return {(entry["path"], entry["rule"]): entry["count"] for entry in entries}


def run(root: Path, *args: str) -> tuple[int, str]:
    """Run the checker's CLI against ``root``, returning (exit code, stdout + stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--root", str(root), *args])
    return code, out.getvalue() + err.getvalue()


def counts_for(source: str, path: str = "tests/test_a.py") -> dict[str, int]:
    """Return rule -> site count for one test module."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {path: source})
        scan = measure(root, ["tests", "libs/*/tests", "sdk/*/tests"])
    return {rule: count for (found, rule), count in scan.counts.items() if found == path}


def test_patch_rule_counts_monkeypatch_mutations_but_not_inputs() -> None:
    source = """
        import os
        import pytest

        def test_a(monkeypatch, tmp_path):
            monkeypatch.setattr(os, "getcwd", None)
            monkeypatch.setitem(os.environ, "A", "1")
            monkeypatch.delattr(os, "getcwd")
            monkeypatch.delitem(os.environ, "A")
            monkeypatch.setenv("A", "1")
            monkeypatch.delenv("A", raising=False)
            monkeypatch.chdir(tmp_path)

        def test_b():
            mp = pytest.MonkeyPatch()
            mp.setattr(os, "getcwd", None)
            with pytest.MonkeyPatch.context() as ctx:
                ctx.setattr(os, "getcwd", None)
    """
    assert counts_for(source) == {"patch": 6}


def test_mock_rule_counts_imports_parameters_and_uses() -> None:
    source = """
        import unittest.mock
        from unittest import mock
        from unittest.mock import MagicMock, patch
        from pytest_mock import MockerFixture

        @patch("os.getcwd")
        def test_a(mocked):
            patch.object(object, "x")
            patch.dict("os.environ", {})
            MagicMock()
            mock.Mock()
            unittest.mock.AsyncMock()

        def test_b(mocker: MockerFixture):
            mocker.patch("os.getcwd")
    """
    # 4 imports, 1 decorator, 5 in-body uses, 1 parameter, 1 mocker call.
    assert counts_for(source) == {"mock": 12}


def test_sleep_rule_allows_only_a_scheduler_yield() -> None:
    source = """
        import asyncio
        import time
        from time import sleep

        async def test_a():
            time.sleep(1)
            sleep(2)
            await asyncio.sleep(0.5)
            await asyncio.sleep(0)
            time.sleep(0)
    """
    assert counts_for(source) == {"sleep": 4}


def test_private_import_rule_applies_only_to_library_tests() -> None:
    source = """
        import vs_x
        import vs_x.api
        import vs_x.api.sub
        import vs_x.core
        from vs_x import thing
        from vs_x.api import thing as public
        from vs_x.api.sub import thing as public_sub
        from vs_x.core import thing as private
        from vs_other import thing as elsewhere
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(
            root,
            {
                "libs/vs-x/src/vs_x/__init__.py": "",
                "libs/vs-x/tests/test_a.py": source,
                "tests/test_b.py": source,
            },
        )
        scan = measure(root, ["tests", "libs/*/tests"])
    assert scan.counts == {("libs/vs-x/tests/test_a.py", "private_import"): 4}


def test_fixtures_and_pycache_directories_are_skipped() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(
            root,
            {
                "tests/fixtures/data.py": f"{SLEEP_SITE}\n",
                "tests/__pycache__/x.py": f"{SLEEP_SITE}\n",
                "tests/test_a.py": "import time\nx = 1\n",
            },
        )
        assert measure(root, ["tests"]).counts == {}


@pytest.mark.parametrize(
    "source",
    [
        "def test_a(monkeypatch):\n    monkeypatch.setattr(o, 'x', 1)  # test-isolation: env seam\n",
        "def test_a(monkeypatch):\n    # test-isolation: env seam\n    monkeypatch.setattr(o, 'x', 1)\n",
    ],
)
def test_exemption_on_the_line_or_the_line_above_is_honored(source: str) -> None:
    assert counts_for(source) == {}


def test_trailing_comment_on_the_previous_statement_does_not_exempt() -> None:
    source = (
        "def test_a(monkeypatch):\n"
        "    x = 1  # test-isolation: about x only\n"
        "    monkeypatch.setattr(o, 'x', 1)\n"
    )
    assert counts_for(source) == {"patch": 1}


def test_empty_exemption_always_fails_and_blocks_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/test_a.py": "x = 1  # test-isolation:   \n"})
        write_baseline(root, [])

        code, output = run(root)
        assert code == EXIT_VIOLATIONS
        assert "tests/test_a.py:1" in output

        code, output = run(root, "--write")
        assert code == EXIT_VIOLATIONS
        assert "tests/test_a.py:1" in output


def test_check_passes_at_baseline_and_reports_the_tightenable_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/test_a.py": f"{PATCH_SITE}\n"})

        write_baseline(root, [("tests/test_a.py", "patch", 1)])
        assert run(root)[0] == EXIT_OK

        write_baseline(root, [("tests/test_a.py", "patch", 3)])
        code, output = run(root)
        assert code == EXIT_OK
        assert "lower the recorded count" in output
        assert "tests/test_a.py: patch: 3 -> 1" in output


def test_check_fails_when_a_count_exceeds_its_baseline() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/test_a.py": f"{PATCH_SITE}\n{PATCH_SITE}\n"})
        write_baseline(root, [("tests/test_a.py", "patch", 1)])

        code, output = run(root)

    assert code == EXIT_VIOLATIONS
    assert "tests/test_a.py: patch x2 > 1 (baseline)" in output
    assert "Fake" in output


def test_check_fails_for_a_site_with_no_baseline_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/test_a.py": f"import time\n{SLEEP_SITE}\n"})
        write_baseline(root, [])

        code, output = run(root)

    assert code == EXIT_VIOLATIONS
    assert "tests/test_a.py: sleep x1, not in baseline" in output


def test_check_fails_on_stale_entries_for_a_clean_or_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/clean.py": "x = 1\n"})
        write_baseline(root, [("tests/clean.py", "mock", 2), ("tests/gone.py", "sleep", 1)])

        code, output = run(root)

    assert code == EXIT_VIOLATIONS
    assert "tests/clean.py: mock" in output
    assert "tests/gone.py: sleep" in output


def test_missing_baseline_or_config_is_a_tool_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/test_a.py": "x = 1\n"})
        assert run(root)[0] == EXIT_TOOL_ERROR
        (root / "pyproject.toml").write_text("[tool]\n", encoding="utf-8")
        assert run(root, "--write")[0] == EXIT_TOOL_ERROR


def test_write_bootstraps_a_missing_baseline_then_check_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/test_a.py": f"import time\n{SLEEP_SITE}\n{PATCH_SITE}\n"})

        assert run(root, "--write")[0] == EXIT_OK
        assert read_baseline(root) == {
            ("tests/test_a.py", "sleep"): 1,
            ("tests/test_a.py", "patch"): 1,
        }
        assert run(root)[0] == EXIT_OK


def test_write_refuses_to_grow_a_count_or_add_an_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/test_a.py": f"{PATCH_SITE}\n{PATCH_SITE}\nimport time\n"})
        write_baseline(root, [("tests/test_a.py", "patch", 1)])
        before = (root / BASELINE).read_text(encoding="utf-8")

        code, output = run(root, "--write")

        assert code == EXIT_VIOLATIONS
        assert "tests/test_a.py: patch x2 > 1" in output
        assert (root / BASELINE).read_text(encoding="utf-8") == before

        make_repo(root, {"tests/test_a.py": f"{PATCH_SITE}\nimport time\n{SLEEP_SITE}\n"})
        code, output = run(root, "--write")
        assert code == EXIT_VIOLATIONS
        assert "tests/test_a.py: sleep x1 > 0" in output


def test_write_shrinks_and_drops_stale_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/test_a.py": f"{PATCH_SITE}\n"})
        write_baseline(root, [("tests/test_a.py", "patch", 4), ("tests/gone.py", "mock", 1)])

        assert run(root, "--write")[0] == EXIT_OK
        assert read_baseline(root) == {("tests/test_a.py", "patch"): 1}


def flagged_and_exempt_source(flagged: int, exempt: int) -> str:
    """Build a module with ``flagged`` counted sites and ``exempt`` exempted ones."""
    lines = ["def test_a(monkeypatch):"]
    lines += [f"    {PATCH_SITE}" for _ in range(flagged)]
    lines += [f"    {PATCH_SITE}  # test-isolation: seam {index}" for index in range(exempt)]
    lines.append("    pass")
    return "\n".join(lines) + "\n"


@CHEAP
@given(flagged=st.integers(0, 12), exempt=st.integers(0, 12))
def test_reported_count_equals_the_number_of_non_exempt_sites(flagged: int, exempt: int) -> None:
    source = flagged_and_exempt_source(flagged, exempt)
    assert counts_for(source).get("patch", 0) == flagged


@CHEAP
@given(initial=st.integers(1, 10), removed=st.integers(0, 10), exempted=st.integers(0, 10))
def test_write_after_removing_sites_shrinks_and_then_passes(
    initial: int, removed: int, exempted: int
) -> None:
    remaining = max(initial - removed, 0)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/test_a.py": flagged_and_exempt_source(initial, 0)})
        assert run(root, "--write")[0] == EXIT_OK

        make_repo(root, {"tests/test_a.py": flagged_and_exempt_source(remaining, exempted)})
        assert run(root, "--write")[0] == EXIT_OK

        recorded = read_baseline(root).get(("tests/test_a.py", "patch"), 0)
        assert recorded == remaining
        assert run(root)[0] == EXIT_OK


@CHEAP
@given(baseline=st.integers(1, 10), current=st.integers(0, 12))
def test_check_result_follows_the_ratchet(baseline: int, current: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, {"tests/test_a.py": flagged_and_exempt_source(current, 0)})
        write_baseline(root, [("tests/test_a.py", "patch", baseline)])

        code, _ = run(root)

    assert (code == EXIT_OK) == (1 <= current <= baseline)
