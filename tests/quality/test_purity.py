"""Executable D203 checker falsification fixtures, through its public functions."""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from scripts.check_purity import (
    PURE_SCOPE,
    BaselineError,
    Violation,
    compare_baseline,
    read_baseline,
    scan_source,
    scan_value_exports,
)


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("import pathlib as p", "banned-import"),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from vs_runtime.api import Runtime",
            "io-import",
        ),
        ("from vs_project.wiring import factory", "implementation-import"),
        ("from vs_project import wiring", "implementation-import"),
        ("from builtins import open as access\naccess('x')", "builtin"),
        ("import builtins as b\nx = b.print", "builtin"),
        ("access = open", "builtin"),
        ("async def f():\n    await result", "await"),
        ("async def f():\n    async for x in stream:\n        pass", "async-for"),
        ("async def f():\n    async with resource:\n        pass", "async-with"),
        ('backend = "modal"', "implementation-literal"),
        ("import uuid as identity", "banned-import"),
        ("import multiprocessing", "banned-import"),
        ("import tempfile", "banned-import"),
    ],
)
def test_forbidden_syntax_aliases_and_type_checking_are_detected(source: str, rule: str) -> None:
    assert rule in {site.rule for site in scan_source("strategy.py", source)}


def test_pure_value_libraries_and_ordinary_strategy_text_are_allowed() -> None:
    assert (
        scan_source(
            "strategy.py",
            'import vs_prompts.api\nimport vs_evaluator_protocol.api\nlabel = "multimodal strategy"\n',
        )
        == ()
    )


def test_syntax_errors_fail_closed() -> None:
    with pytest.raises(SyntaxError):
        scan_source("strategy.py", "def invalid(:")


@given(st.integers(min_value=0, max_value=100))
def test_line_shifts_do_not_change_ratchet_identity(blank_lines: int) -> None:
    source = "class Owner:\n    async def decide(self):\n        await request\n"
    before = scan_source("strategy.py", source)
    assert scan_source("strategy.py", "\n" * blank_lines + source) == before
    assert {site.symbol for site in before} == {"Owner.decide"}


def test_new_stale_growth_and_pure_waivers_each_fail() -> None:
    site = Violation("src/vibesys/orchestration/strategy.py", "decide", "await", "request", 1)
    one = frozenset({site})
    empty = frozenset()
    assert compare_baseline(one, empty, empty)[0].startswith("new:")
    assert compare_baseline(empty, one, one)[0].startswith("stale:")
    assert compare_baseline(one, one, empty)[0].startswith("baseline growth:")
    assert compare_baseline(one, one, one) == ()
    pure = frozenset({Violation(f"{PURE_SCOPE}/new.py", "decide", "await", "request", 1)})
    assert compare_baseline(pure, pure, pure)[0].startswith("pure waiver:")


def test_unknown_baseline_keys_and_duplicate_sites_are_rejected() -> None:
    with pytest.raises(TypeError):
        read_baseline('[{"path":"x","unknown":true}]')
    with pytest.raises(BaselineError, match="duplicate"):
        read_baseline(
            '[{"path":"x","symbol":"f","rule":"await","subject":"x","occurrence":1},{"path":"x","symbol":"f","rule":"await","subject":"x","occurrence":1}]'
        )


def test_request_value_exports_allow_values_but_reject_transitive_io(tmp_path: Path) -> None:
    requests = tmp_path / "libs" / "vs-project" / "src" / "vs_project" / "api" / "requests.py"
    requests.parent.mkdir(parents=True)
    requests.write_text("from ..values import Payload\n")
    values = requests.parent.parent / "values.py"
    values.write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import asyncio\n")
    violations = scan_value_exports(tmp_path)
    assert len(violations) == 1
    assert violations[0].path.endswith("vs_project/values.py")
    assert violations[0].rule == "banned-import"
    values.write_text("from pydantic import BaseModel\nclass Payload(BaseModel):\n    value: str\n")
    assert scan_value_exports(tmp_path) == ()
    assert scan_source("strategy.py", "from vs_project.api.requests import ArtifactPut") == ()
    assert scan_source(f"{PURE_SCOPE}/bad.py", "from vs_project.api.requests import ArtifactPut")


def test_request_export_closure_checks_executed_parent_packages(tmp_path: Path) -> None:
    api = tmp_path / "libs" / "values" / "src" / "values" / "api"
    api.mkdir(parents=True)
    (api / "requests.py").write_text("class Request: pass\n")
    (api / "__init__.py").write_text("import socket\n")
    violations = scan_value_exports(tmp_path)
    assert len(violations) == 1
    assert violations[0].path.endswith("values/api/__init__.py")
    assert violations[0].subject == "socket"
