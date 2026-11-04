"""The test selection the pre-push gate and CI's commit stage share follows imports and a budget."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from scripts import impacted_tests as it

from vs_sim_pytest import DEFAULT_REAL_TIERS

if TYPE_CHECKING:
    from collections.abc import Mapping

World = tuple[dict[str, str], dict[int, set[int]], dict[int, set[int]]]

TESTPATHS = ["tests", "libs/lib/tests"]
WORKERS = 4


def _module(index: int) -> str:
    return f"libs/lib/src/pkg/m{index}.py"


def _test(index: int) -> str:
    return f"libs/lib/tests/test_{index}.py"


def _import_line(index: int, *, style: bool) -> str:
    return f"from pkg import m{index}\n" if style else f"import pkg.m{index}\n"


@st.composite
def _worlds(draw: st.DrawFn) -> World:
    """Sources for modules m0..mN and tests t0..tM, with the import edges that were written."""
    modules = draw(st.integers(min_value=1, max_value=7))
    tests = draw(st.integers(min_value=1, max_value=5))
    module_edges = {
        i: draw(
            st.sets(st.integers(min_value=0, max_value=modules - 1), max_size=3).filter(
                lambda s, i=i: i not in s
            )
        )
        for i in range(modules)
    }
    test_edges = {
        i: draw(st.sets(st.integers(min_value=0, max_value=modules - 1), max_size=3))
        for i in range(tests)
    }
    style = draw(st.booleans())
    sources = {
        _module(i): "".join(_import_line(d, style=style) for d in sorted(edges))
        for i, edges in module_edges.items()
    }
    sources.update(
        {
            _test(i): "".join(_import_line(d, style=style) for d in sorted(edges))
            for i, edges in test_edges.items()
        }
    )
    return sources, module_edges, test_edges


def _oracle(
    changed: set[int], module_edges: Mapping[int, set[int]], test_edges: Mapping[int, set[int]]
) -> dict[str, int]:
    """Distances by relaxing every edge until none improves, a different walk from the BFS."""
    distance: dict[str, int] = {f"pkg.m{i}": 0 for i in changed}
    edges = {f"pkg.m{i}": {f"pkg.m{d}" for d in deps} for i, deps in module_edges.items()}
    edges.update(
        {it.module_name(_test(i)): {f"pkg.m{d}" for d in deps} for i, deps in test_edges.items()}
    )
    moved = True
    while moved:
        moved = False
        for importer, deps in edges.items():
            best = min((distance[d] for d in deps if d in distance), default=None)
            if best is not None and best + 1 < distance.get(importer, 10**9):
                distance[importer] = best + 1
                moved = True
    return distance


@given(world=_worlds(), data=st.data())
def test_a_test_is_impacted_exactly_when_imports_reach_a_changed_module(
    world: World, data: st.DataObject
) -> None:
    sources, module_edges, test_edges = world
    changed = data.draw(
        st.sets(st.integers(min_value=0, max_value=len(module_edges) - 1), min_size=1)
    )
    reached = it.impacted([_module(i) for i in changed], it.importers(sources, TESTPATHS))
    assert reached == _oracle(changed, module_edges, test_edges)


@given(
    world=_worlds(),
    data=st.data(),
    budget=st.floats(min_value=0.5, max_value=40, allow_nan=False),
    seconds=st.lists(
        st.floats(min_value=0.1, max_value=30, allow_nan=False), min_size=5, max_size=5
    ),
)
def test_selection_covers_the_impacted_tests_once_and_stays_in_budget(
    world: World, data: st.DataObject, budget: float, seconds: list[float]
) -> None:
    sources, module_edges, test_edges = world
    changed = data.draw(
        st.sets(st.integers(min_value=0, max_value=len(module_edges) - 1), min_size=1)
    )
    touched = data.draw(st.sets(st.sampled_from(sorted(test_edges)), max_size=2))
    paths = [_module(i) for i in changed] + [_test(i) for i in touched]
    durations = {_test(i): seconds[i] for i in test_edges}
    selection = it.select(
        paths, sources, durations, testpaths=TESTPATHS, budget=it.Budget(budget, WORKERS)
    )

    reached = _oracle(changed, module_edges, test_edges)
    reached.update({it.module_name(_test(i)): 0 for i in touched})
    impacted = {_test(i) for i in test_edges if it.module_name(_test(i)) in reached}
    assert set(selection.files) | set(selection.left_out) == impacted
    assert not set(selection.files) & set(selection.left_out)
    assert {_test(i) for i in touched} <= set(selection.files)
    unavoidable = sum(durations[_test(i)] for i in touched)
    spent = sum(durations[path] for path in selection.files)
    assert spent <= max(budget * WORKERS, unavoidable) + 1e-9


def test_a_conftest_is_imported_by_every_test_below_it_and_no_other() -> None:
    sources = {
        "tests/vibesys/conftest.py": "",
        "tests/vibesys/test_a.py": "",
        "tests/vibesys/deeper/test_b.py": "",
        "tests/server/test_c.py": "",
    }
    selection = it.select(
        ["tests/vibesys/conftest.py"],
        sources,
        {},
        testpaths=["tests"],
        budget=it.Budget(100, WORKERS),
    )
    assert selection.files == ("tests/vibesys/deeper/test_b.py", "tests/vibesys/test_a.py")


def test_a_relative_import_and_a_package_init_reach_their_importers() -> None:
    sources = {
        "libs/lib/src/pkg/__init__.py": "",
        "libs/lib/src/pkg/sub/__init__.py": "from . import leaf\n",
        "libs/lib/src/pkg/sub/leaf.py": "from .. import helper\n",
        "libs/lib/src/pkg/helper.py": "",
        "libs/lib/tests/test_leaf.py": "from pkg.sub import leaf\n",
        "libs/lib/tests/test_unrelated.py": "import json\n",
    }
    by_helper = it.select(
        ["libs/lib/src/pkg/helper.py"],
        sources,
        {},
        testpaths=TESTPATHS,
        budget=it.Budget(100, WORKERS),
    )
    by_init = it.select(
        ["libs/lib/src/pkg/sub/__init__.py"],
        sources,
        {},
        testpaths=TESTPATHS,
        budget=it.Budget(100, WORKERS),
    )
    assert by_helper.files == ("libs/lib/tests/test_leaf.py",)
    assert by_init.files == ("libs/lib/tests/test_leaf.py",)


def test_a_deleted_module_still_selects_the_tests_that_import_it() -> None:
    sources = {"libs/lib/tests/test_old.py": "import pkg.gone\n"}
    selection = it.select(
        ["libs/lib/src/pkg/gone.py"],
        sources,
        {},
        testpaths=TESTPATHS,
        budget=it.Budget(100, WORKERS),
    )
    assert selection.files == ("libs/lib/tests/test_old.py",)


def test_the_real_system_tiers_are_never_selected() -> None:
    sources = {f"{tier}/test_x.py": "import pkg.m0\n" for tier in it.REAL_TIERS}
    sources["tests/test_ok.py"] = "import pkg.m0\n"
    selection = it.select(
        [_module(0)], sources, {}, testpaths=["tests"], budget=it.Budget(100, WORKERS)
    )
    assert selection.files == ("tests/test_ok.py",)


def test_the_real_tiers_match_the_pytest_plugin() -> None:
    assert set(it.REAL_TIERS) == set(DEFAULT_REAL_TIERS)


def test_a_file_that_does_not_parse_selects_nothing_and_stops_nothing() -> None:
    sources = {"tests/test_bad.py": "def (:\n", "tests/test_good.py": "import pkg.m0\n"}
    selection = it.select(
        [_module(0)], sources, {}, testpaths=["tests"], budget=it.Budget(100, WORKERS)
    )
    assert selection.files == ("tests/test_good.py",)


def test_module_names_follow_the_source_roots_pytest_uses() -> None:
    assert it.module_name("libs/vs-core/src/vs_core/api.py") == "vs_core.api"
    assert it.module_name("src/vibesys/api/__init__.py") == "vibesys.api"
    assert it.module_name("tests/support/sharding.py") == "tests.support.sharding"
    assert it.module_name("packaging/fetch_bun.py") == "fetch_bun"
    assert it.module_name("libs/vs-sim/pytest_plugin/vs_sim_pytest.py") == "vs_sim_pytest"
