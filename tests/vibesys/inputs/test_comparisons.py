"""``[[comparison]]`` tables: comparison baselines declared by a task, validated at the edge."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.task_project import task_path, write_task

from vibesys.inputs import InputManifest, load_task, render_input_manifest
from vs_project.api import ComparisonInput

if TYPE_CHECKING:
    from pathlib import Path

_NAMES = st.from_regex(r"[a-z][a-z0-9_-]{0,31}", fullmatch=True)
_LABELS = (
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 .-", min_size=1, max_size=40)
    .map(str.strip)
    .filter(bool)
)
_DIGESTS = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
_IMAGES = st.builds(
    lambda repo, digest: f"docker://{repo}@sha256:{digest}",
    st.from_regex(r"[a-z0-9]+(/[a-z0-9]+){0,2}", fullmatch=True),
    _DIGESTS,
)
_DOCKERFILES = st.from_regex(r"[a-z][a-z0-9_]{0,8}(/[a-z][a-z0-9_]{0,8}){0,2}", fullmatch=True)
_ARGV = st.lists(st.text(alphabet="abcdefghij-=/.", min_size=1, max_size=12), max_size=4).map(tuple)
_ENV = st.dictionaries(
    st.from_regex(r"[A-Z_][A-Z0-9_]{0,10}", fullmatch=True),
    st.text(alphabet="abc 123/:.", max_size=10),
    max_size=3,
)


@st.composite
def _comparisons(draw: st.DrawFn) -> dict[str, Any]:
    table: dict[str, Any] = {
        "name": draw(_NAMES),
        "label": draw(_LABELS),
        "serve": [*draw(_ARGV), "--port=${PORT}", *draw(_ARGV)],
        "env": draw(_ENV),
        "benchmark_args": list(draw(_ARGV)),
        "ready_timeout_seconds": draw(st.integers(1, 10_000)),
        "visible_to_agents": draw(st.booleans()),
    }
    if draw(st.booleans()):
        table["image"] = draw(_IMAGES)
    else:
        table["dockerfile"] = draw(_DOCKERFILES)
    return table


def _manifest(*comparisons: dict[str, Any], interface: str | None = "service") -> dict[str, Any]:
    agent: dict[str, Any] = {"domain": "generic"}
    if interface is not None:
        agent["interface"] = interface
    return {
        "version": 1,
        "agent": agent,
        "accuracy": {"command": ["check"]},
        "benchmark": {"command": ["measure"]},
        "comparison": list(comparisons),
    }


_DIGEST = "a" * 64
_IMAGE = f"docker://rocm/vllm@sha256:{_DIGEST}"


def _valid(**overrides: object) -> dict[str, Any]:
    return {
        "name": "vllm",
        "label": "vLLM",
        "image": _IMAGE,
        "serve": ["vllm", "serve", "--port", "${PORT}"],
        **overrides,
    }


@given(
    tables=st.lists(
        _comparisons(), max_size=3, unique_by=(lambda t: t["name"], lambda t: t["label"])
    )
)
def test_comparisons_round_trip_through_the_rendered_manifest(tables: list[dict[str, Any]]) -> None:
    manifest = InputManifest.model_validate(_manifest(*tables))

    reread = InputManifest.model_validate(tomllib.loads(render_input_manifest(manifest)))

    assert reread.comparison == manifest.comparison
    assert [c.name for c in reread.comparison] == [t["name"] for t in tables]


def test_a_task_without_comparisons_declares_none() -> None:
    assert InputManifest.model_validate(_manifest(interface=None)).comparison == ()


@given(name=_NAMES, image=_IMAGES, dockerfile=_DOCKERFILES)
@example(name="vllm", image=_IMAGE, dockerfile="docker/vllm")
def test_a_comparison_declares_exactly_one_image_source_and_the_error_names_it(
    name: str, image: str, dockerfile: str
) -> None:
    both = _valid(name=name, image=image, dockerfile=dockerfile)
    neither = {k: v for k, v in _valid(name=name).items() if k != "image"}

    for table, what in ((both, "both"), (neither, "neither")):
        with pytest.raises(ValidationError, match=rf"comparison '{name}'.*got {what}"):
            ComparisonInput.model_validate(table)


@given(name=_NAMES, key=st.from_regex(r"[a-z]{3,10}", fullmatch=True))
def test_an_unknown_key_is_rejected_naming_it(name: str, key: str) -> None:
    if key in ComparisonInput.model_fields:
        return
    with pytest.raises(ValidationError, match=key):
        ComparisonInput.model_validate(_valid(name=name, **{key: "x"}))


@pytest.mark.parametrize(
    "image",
    [
        "rocm/vllm@sha256:" + _DIGEST,
        "docker://rocm/vllm:latest",
        "docker://rocm/vllm@sha256:" + "a" * 63,
        "docker://rocm/vllm@sha256:" + "A" * 64,
        "docker://rocm/vllm:v0.1@md5:" + _DIGEST,
        "",
    ],
)
def test_an_image_must_be_pinned_by_digest(image: str) -> None:
    with pytest.raises(ValidationError, match="image"):
        ComparisonInput.model_validate(_valid(image=image))


@pytest.mark.parametrize(
    "dockerfile", ["/abs/Dockerfile", "../Dockerfile", "a/../../Dockerfile", " "]
)
def test_a_dockerfile_stays_inside_the_task(dockerfile: str) -> None:
    table = {k: v for k, v in _valid().items() if k != "image"}
    with pytest.raises(ValidationError, match="dockerfile"):
        ComparisonInput.model_validate({**table, "dockerfile": dockerfile})


@pytest.mark.parametrize(
    ("serve", "message"),
    [
        (["vllm", "serve"], r"must contain \$\{PORT\}"),
        (["vllm", "--port", "${PORT}", "--model", "${MODEL}"], r"\$\{MODEL\}"),
        (["vllm", "${PORT}", ""], "non-empty"),
        ([], "at least 1"),
    ],
)
def test_serve_takes_its_port_from_the_one_placeholder(serve: list[str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ComparisonInput.model_validate(_valid(serve=serve))


@given(port=st.integers(1, 65535), comparison=_comparisons().map(ComparisonInput.model_validate))
def test_serve_argv_substitutes_the_port_everywhere(port: int, comparison: ComparisonInput) -> None:
    argv = comparison.serve_argv(port)

    assert len(argv) == len(comparison.serve)
    assert "${PORT}" not in "".join(argv)
    assert f"--port={port}" in argv


@pytest.mark.parametrize("name", ["", "VLLM", "v llm", "1vllm", "a" * 33, "v/llm"])
def test_a_name_is_a_short_lowercase_identifier(name: str) -> None:
    with pytest.raises(ValidationError, match="name"):
        ComparisonInput.model_validate(_valid(name=name))


@pytest.mark.parametrize("label", ["", " vLLM", "vLLM\n", "x" * 41])
def test_a_label_is_one_short_printable_line(label: str) -> None:
    with pytest.raises(ValidationError, match="label"):
        ComparisonInput.model_validate(_valid(label=label))


@pytest.mark.parametrize("key", ["", "1A", "A-B", "A B"])
def test_env_keys_are_variable_names(key: str) -> None:
    with pytest.raises(ValidationError, match="env keys"):
        ComparisonInput.model_validate(_valid(env={key: "x"}))


def test_the_ready_timeout_is_positive() -> None:
    with pytest.raises(ValidationError, match="ready_timeout_seconds"):
        ComparisonInput.model_validate(_valid(ready_timeout_seconds=0))


def test_a_comparison_is_hidden_from_agents_unless_it_says_otherwise() -> None:
    assert ComparisonInput.model_validate(_valid()).visible_to_agents is False
    assert ComparisonInput.model_validate(_valid(visible_to_agents=True)).visible_to_agents is True
    with pytest.raises(ValidationError, match="visible_to_agents"):
        ComparisonInput.model_validate(_valid(visible_to_agents="yes"))


@pytest.mark.parametrize("key", ["name", "label"])
def test_names_and_labels_are_distinct_across_comparisons(key: str) -> None:
    first = _valid(name="a", label="A")
    second = {**_valid(name="b", label="B"), key: first[key]}

    with pytest.raises(
        ValidationError, match=rf"comparison {key} '{first[key]}' is declared twice"
    ):
        InputManifest.model_validate(_manifest(first, second))


@pytest.mark.parametrize("interface", [None, "inprocess"])
def test_comparisons_need_a_service_task(interface: str | None) -> None:
    with pytest.raises(ValidationError, match=r"comparison 'vllm'.*interface = \"service\""):
        InputManifest.model_validate(_manifest(_valid(), interface=interface))


_TASK_TOML = """version = 1
[agent]
domain = "generic"
interface = "service"
[accuracy]
command = ["python", "acc.py"]
[benchmark]
command = ["measure"]

[[comparison]]
name = "vllm"
label = "vLLM"
dockerfile = "{dockerfile}"
serve = ["vllm", "--port", "${{PORT}}"]
"""


def test_a_comparison_dockerfile_must_be_a_file_in_the_task(tmp_path: Path) -> None:
    task = write_task(tmp_path, _TASK_TOML.format(dockerfile="baselines/vllm.Dockerfile"))
    with pytest.raises(FileNotFoundError, match=r"comparison 'vllm'.*baselines/vllm\.Dockerfile"):
        load_task(tmp_path)

    (task / "baselines").mkdir()
    (task / "baselines" / "vllm.Dockerfile").write_text("FROM scratch\n")

    assert load_task(tmp_path).manifest.comparison[0].dockerfile == "baselines/vllm.Dockerfile"


def test_a_comparison_never_builds_the_tasks_own_image(tmp_path: Path) -> None:
    task = write_task(tmp_path, _TASK_TOML.format(dockerfile="Dockerfile"))
    (task / "Dockerfile").write_text("FROM scratch\n")

    with pytest.raises(ValueError, match=r"comparison 'vllm'.*task's own image"):
        load_task(tmp_path)
    assert task_path(tmp_path) == task
