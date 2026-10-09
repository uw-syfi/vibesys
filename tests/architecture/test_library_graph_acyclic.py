"""The libraries form a DAG at package granularity.

Tach models fine-grained modules, so it accepts a cycle that runs through three
libraries as long as no module-level edge closes it. Separately declared
distributions cannot depend on each other in a cycle, so this test reads the
imports of every `libs/*/src` tree and fails on any cycle between whole
libraries.
"""

from __future__ import annotations

import ast
from graphlib import CycleError, TopologicalSorter
from itertools import pairwise
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIBRARY_PACKAGE_PREFIX = "vs_"


def library_import_graph(libs_root: Path) -> dict[str, set[str]]:
    """Map each library's import package to the other library packages it imports."""
    sources = {
        package.name: package
        for library in sorted(libs_root.iterdir())
        if (library / "src").is_dir()
        for package in (library / "src").iterdir()
        if package.is_dir() and package.name.startswith(LIBRARY_PACKAGE_PREFIX)
    }
    graph: dict[str, set[str]] = {name: set() for name in sources}
    for name, package in sources.items():
        for path in sorted(package.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported = [node.module]
                else:
                    continue
                graph[name].update(
                    top for top in (item.partition(".")[0] for item in imported) if top in sources
                )
        graph[name].discard(name)
    return graph


def find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    """Return the nodes of one dependency cycle, or ``None`` when ``graph`` is acyclic."""
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as error:
        return list(error.args[1])
    return None


def test_the_libraries_import_each_other_without_a_cycle() -> None:
    graph = library_import_graph(PROJECT_ROOT / "libs")

    assert len(graph) > 1
    assert find_cycle(graph) is None, f"library import cycle: {find_cycle(graph)}"


def test_the_vs_mcp_leaf_imports_no_other_library() -> None:
    assert library_import_graph(PROJECT_ROOT / "libs")["vs_mcp"] == set()


@given(st.lists(st.integers(0, 8), unique=True, min_size=1), st.data())
def test_a_graph_whose_edges_all_point_down_an_order_has_no_cycle(
    order: list[int], data: st.DataObject
) -> None:
    graph = {
        str(node): {
            str(other)
            for other in data.draw(st.lists(st.sampled_from(order[:position] or [node])))
            if other != node
        }
        for position, node in enumerate(order)
    }

    assert find_cycle(graph) is None


@given(st.lists(st.integers(0, 8), unique=True, min_size=2), st.data())
def test_closing_any_path_back_to_its_start_is_reported_as_a_cycle(
    path: list[int], data: st.DataObject
) -> None:
    graph: dict[str, set[str]] = {str(node): set() for node in path}
    for upper, lower in pairwise(path):
        graph[str(upper)].add(str(lower))
    start = data.draw(st.integers(0, len(path) - 2))
    graph[str(path[-1])].add(str(path[start]))

    cycle = find_cycle(graph)

    assert cycle is not None
    assert set(cycle) <= {str(node) for node in path[start:]}
