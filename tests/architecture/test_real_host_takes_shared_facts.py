"""The helper's host takes every fact a read also answers from out of ``HostFacts``.

``HostFacts`` is the one wiring of those facts (see ``tests/entrypoints/test_host_facts.py`` for
what a built fact answers). A fact the helper wired itself, with a collaborator of the same class,
answers alike today and drifts the day one side changes its probe or its arguments. This test
reads ``real_host`` and fails on a shared fact it builds itself, naming the line.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source

from entrypoints.host_facts import HostFacts
from server.host_helper_requests import HelperHost

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

HELPER = Path(__file__).resolve().parents[2] / "src" / "entrypoints" / "host_helper.py"
SHARED = frozenset(
    {field.name for field in dataclasses.fields(HelperHost)}
    & {name for name in dir(HostFacts) if not name.startswith("_")}
)
"""The facts both hosts have, found from the two public surfaces."""


def _fact_of(node: ast.expr, facts: str) -> str | None:
    """``name`` for ``facts.name``, else ``None``."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == facts
    ):
        return node.attr
    return None


def rewired_facts(
    source: str, shared: frozenset[str], filename: str = "host_helper.py"
) -> list[str]:
    """Each shared fact ``real_host`` hands ``HelperHost`` without taking it from ``facts``."""
    tree = parse_source(source, filename)
    real_host = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "real_host"
    )
    aliases = {
        target.id: fact
        for node in ast.walk(real_host)
        if isinstance(node, ast.Assign) and (fact := _fact_of(node.value, "facts")) is not None
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    problems: list[str] = []
    for call in ast.walk(real_host):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "HelperHost"
        ):
            continue
        for keyword in call.keywords:
            if keyword.arg not in shared:
                continue
            value = keyword.value
            taken = _fact_of(value, "facts") or (
                aliases.get(value.id) if isinstance(value, ast.Name) else None
            )
            if taken != keyword.arg:
                problems.append(
                    f"{filename}:{keyword.value.lineno}: real_host wires `{keyword.arg}` itself; "
                    f"take it from HostFacts (`{keyword.arg}=facts.{keyword.arg}`) so a read "
                    "builds it the same way"
                )
    return problems


_COMPLIANT = """
def real_host(environ, uid, threads):
    facts = HostFacts(environ, uid)
    epoch = facts.clock
    return HelperHost(registry=facts.registry, compute=facts.compute, clock=epoch, readers=Readers())
"""


def test_a_host_that_takes_its_shared_facts_from_host_facts_is_accepted() -> None:
    assert rewired_facts(_COMPLIANT, frozenset({"registry", "compute", "clock"})) == []


@pytest.mark.parametrize("fact", ["registry", "compute", "clock"])
def test_a_shared_fact_the_host_builds_itself_is_rejected_naming_it_and_the_line(fact: str) -> None:
    source = _COMPLIANT.replace(
        {
            "registry": "registry=facts.registry",
            "compute": "compute=facts.compute",
            "clock": "clock=epoch",
        }[fact],
        f"{fact}=Something()",
    )

    [problem] = rewired_facts(source, frozenset({"registry", "compute", "clock"}))

    assert problem.startswith("host_helper.py:5:")
    assert f"`{fact}`" in problem
    assert "HostFacts" in problem


def test_a_fact_taken_from_another_fact_is_rejected() -> None:
    source = _COMPLIANT.replace("compute=facts.compute", "compute=facts.registry")

    [problem] = rewired_facts(source, frozenset({"registry", "compute"}))

    assert "`compute`" in problem


def test_the_helpers_real_host_takes_every_shared_fact_from_host_facts() -> None:
    assert {"registry", "projects", "compute", "logins"} <= SHARED
    assert rewired_facts(HELPER.read_text(encoding="utf-8"), SHARED, str(HELPER)) == []
