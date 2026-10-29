"""The example catalogue's task facts: what ``[catalogue]``, ``objectives.toml`` and the goal say."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.example_materializer import FakeExampleMaterializer

from entrypoints.example_catalog import CatalogError, list_examples
from entrypoints.examples import HelperExampleProjects
from server.host_api import (
    ExamplesListReply,
    ExamplesListRequest,
    HelperFeature,
    request_features,
)
from server.host_examples import GOAL_MAX_CHARS, ExampleList, bounded_goal
from server.host_preflight import Backend
from vs_project.api import (
    AcceleratorVendor,
    CatalogueInput,
    ExampleDomain,
    ModelMaker,
)
from vs_sim.api.testing import examples

_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters='"\\'),
    min_size=1,
    max_size=30,
)
_GOALS = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\r"), max_size=50
)
_BACKENDS = st.sampled_from([backend.value for backend in Backend])


@st.composite
def _catalogues(draw: st.DrawFn) -> dict[str, object]:
    """A valid ``[catalogue]`` table: its facts match its domain."""
    domain = draw(st.sampled_from(ExampleDomain))
    table: dict[str, object] = {
        "domain": domain.value,
        "hardware": {
            "accelerator": draw(_TEXT),
            "count": draw(st.integers(min_value=1, max_value=4096)),
            "vendor": draw(st.sampled_from(AcceleratorVendor)).value,
        },
    }
    if domain is ExampleDomain.LLM_SERVING:
        table["model"] = {"name": draw(_TEXT), "maker": draw(st.sampled_from(ModelMaker)).value}
    if domain is ExampleDomain.DATA_STRUCTURES:
        table["workload"] = {"name": draw(_TEXT), "description": draw(_TEXT)}
    return table


def _toml(value: object, path: tuple[str, ...] = ()) -> str:
    """Render ``value`` (nested dicts of strings and ints) as TOML tables."""
    assert isinstance(value, dict)
    lines = [f"[{'.'.join(path)}]"]
    nested = []
    for key, item in value.items():
        if isinstance(item, dict):
            nested.append(_toml(item, (*path, key)))
        else:
            lines.append(f"{key} = {json.dumps(item, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n" + "".join(nested)


def _checkout(root: Path, input_text: str, *, goal: str, objectives: str | None) -> Path:
    example = root / "examples" / "area" / "demo"
    task = example / ".vibesys" / "tasks" / "main"
    task.mkdir(parents=True)
    (task / "vibesys.input.toml").write_text(input_text, encoding="utf-8")
    (task / "OBJECTIVE.md").write_text(goal, encoding="utf-8")
    if objectives is not None:
        (task / "objectives.toml").write_text(objectives, encoding="utf-8")
    (root / "examples" / "registry.toml").write_text(
        'version = 1\n\n[[example]]\npath = "examples/area/demo"\n', encoding="utf-8"
    )
    return root


@settings(
    max_examples=examples(pr=30, nightly=300),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    table=_catalogues(),
    backend=_BACKENDS,
    goal=_GOALS,
    axes=st.lists(
        st.tuples(_TEXT, st.sampled_from(["max", "min"])), max_size=3, unique_by=lambda a: a[0]
    ),
)
def test_a_task_lists_every_fact_its_files_declare(
    tmp_path_factory: pytest.TempPathFactory,
    table: dict[str, object],
    backend: str,
    goal: str,
    axes: list[tuple[str, str]],
) -> None:
    root = tmp_path_factory.mktemp("checkout")
    objectives = "".join(
        f'[[objective]]\nname = {json.dumps(name, ensure_ascii=False)}\ndirection = "{direction}"\n'
        for name, direction in axes
    )
    _checkout(
        root,
        f'version = 1\nbackends = ["{backend}"]\n\n' + _toml(table, ("catalogue",)),
        goal=goal,
        objectives=objectives or None,
    )

    details = list_examples(root).examples[0].task_details
    assert details is not None
    [detail] = details

    expected = CatalogueInput.model_validate(table)
    assert detail.task == "main"
    assert detail.domain is expected.domain
    assert detail.hardware.model_dump(exclude={"backend"}) == expected.hardware.model_dump()
    assert detail.hardware.backend == Backend(backend)
    assert (detail.model is None) == (expected.model is None)
    assert (detail.workload is None) == (expected.workload is None)
    assert [(o.metric, o.direction) for o in detail.objectives] == axes
    assert detail.goal == goal
    assert not detail.goal_truncated


@given(text=st.text(max_size=3 * GOAL_MAX_CHARS // 2))
@settings(max_examples=examples(pr=30, nightly=200))
def test_a_goal_is_cut_to_its_bound_and_only_when_longer(text: str) -> None:
    goal, truncated = bounded_goal(text)

    assert len(goal) <= GOAL_MAX_CHARS
    assert text.startswith(goal)
    assert truncated == (len(text) > GOAL_MAX_CHARS)
    assert (goal == text) == (not truncated)


def test_a_task_without_a_catalogue_table_is_not_listed(tmp_path: Path) -> None:
    root = _checkout(tmp_path, "version = 1\n", goal="Go.\n", objectives=None)

    summary = list_examples(root).examples[0]

    assert summary.tasks == ("main",)
    assert summary.task_details == ()


@pytest.mark.parametrize(
    "table",
    [
        '[catalogue]\ndomain = "llm-serving"\n[catalogue.hardware]\naccelerator = "A"\ncount = 1\n'
        'vendor = "nvidia"\n',
        '[catalogue]\ndomain = "databases"\nflavour = "x"\n[catalogue.hardware]\naccelerator = "A"\n'
        'count = 1\nvendor = "cpu"\n',
        '[catalogue]\ndomain = "databases"\n[catalogue.hardware]\naccelerator = "A"\ncount = 0\n'
        'vendor = "cpu"\n',
        '[catalogue]\ndomain = "kernels"\n[catalogue.hardware]\naccelerator = "A"\ncount = 1\n'
        'vendor = "cpu"\n',
        '[catalogue]\ndomain = "databases"\n[catalogue.hardware]\naccelerator = "A"\ncount = 1\n'
        'vendor = "cpu"\n[catalogue.workload]\nname = "w"\ndescription = "d"\n',
    ],
    ids=["model-missing", "unknown-key", "zero-count", "unknown-domain", "workload-off-domain"],
)
def test_a_malformed_catalogue_table_names_its_file(tmp_path: Path, table: str) -> None:
    root = _checkout(tmp_path, f"version = 1\n{table}", goal="Go.\n", objectives=None)

    with pytest.raises(CatalogError, match=r"vibesys\.input\.toml"):
        list_examples(root)


def test_the_helper_lists_task_details_only_when_asked(tmp_path: Path) -> None:
    table = (
        '[catalogue]\ndomain = "databases"\n[catalogue.hardware]\naccelerator = "CPU"\n'
        'count = 1\nvendor = "cpu"\n'
    )
    root = _checkout(tmp_path, f"version = 1\n{table}", goal="Go.\n", objectives=None)
    projects = HelperExampleProjects(root, FakeExampleMaterializer())

    plain = ExamplesListReply(request_id="r1", listing=projects.list())
    asked = ExamplesListReply(request_id="r1", listing=projects.list(with_catalogue=True))

    assert "task_details" not in json.loads(plain.model_dump_json())["listing"]["examples"][0]
    [detail] = json.loads(asked.model_dump_json())["listing"]["examples"][0]["task_details"]
    assert detail["goal"] == "Go.\n"
    assert (
        ExampleList.model_validate_json(json.dumps(json.loads(plain.model_dump_json())["listing"]))
        .examples[0]
        .task_details
        is None
    )


def test_asking_for_the_catalogue_needs_its_feature() -> None:
    assert request_features(ExamplesListRequest(request_id="r1", deadline_ms=1000)) == (
        HelperFeature.EXAMPLES,
    )
    assert request_features(
        ExamplesListRequest(request_id="r1", deadline_ms=1000, with_catalogue=True)
    ) == (
        HelperFeature.EXAMPLES,
        HelperFeature.EXAMPLE_CATALOGUE,
    )


def test_the_queue_rs_client_fixture_is_what_the_catalogue_reads_from_the_real_example() -> None:
    """The desktop clients' ``queueRsCatalogue`` fixture is the real example's catalogue, verbatim.

    New run screens are checked on it, so a goal or a workload that changed in the example's task
    files must change the fixture too (``clients/libs/run-fixtures/src/data``).
    """
    root = Path(__file__).resolve().parents[2]
    fixture = json.loads(
        (root / "clients/libs/run-fixtures/src/data/queue-rs-catalogue.json").read_text()
    )
    real = list_examples(root).model_dump(mode="json", exclude_none=False)
    queue = [
        example
        for example in real["examples"]
        if example["name"] == "data-structures/repositories/queue-rs"
    ]

    assert fixture["examples"] == queue
