"""A task's evaluation in the short form (build, check, measure, metric) and the existing form.

Both forms compile to one manifest model, so they compile to the same stages and, through the
task's terms, to the same measured stages of every measurement request.
"""

from __future__ import annotations

import tomllib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.candidates.api import Gates, Measuring, terms_of
from vibesys.inputs import InputManifest, StageKind, render_input_manifest
from vibesys.metrics import MetricSpace
from vs_core.api import ArtifactId, ArtifactRef

_WORD = st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True)
_ARGS = st.lists(_WORD, max_size=4)
_TIMEOUT = st.none() | st.integers(1, 3600)


def _command(entrypoint: str, args: list[str], timeout: int | None) -> dict[str, object]:
    table: dict[str, object] = {"entrypoint": entrypoint, "args": args}
    if timeout is not None:
        table["timeout_seconds"] = timeout
    return table


def _base() -> dict[str, object]:
    return {
        "version": 1,
        "agent": {"domain": "generic"},
        "evaluator": {"name": "vibesys-evaluator-queue", "version": "0.1.0"},
    }


@given(
    build=st.none() | st.lists(_WORD, min_size=1, max_size=3),
    check=_ARGS,
    measure=_ARGS,
    timeouts=st.tuples(_TIMEOUT, _TIMEOUT),
)
def test_both_forms_compile_to_the_same_manifest_and_stages(
    build: list[str] | None,
    check: list[str],
    measure: list[str],
    timeouts: tuple[int | None, int | None],
) -> None:
    common = _base() | ({} if build is None else {"build": {"command": build}})
    short = InputManifest.model_validate(
        common
        | {
            "check": _command("vibesys-queue", check, timeouts[0]),
            "measure": _command("vibesys-queue", measure, timeouts[1]),
        }
    )
    existing = InputManifest.model_validate(
        common
        | {
            "accuracy": _command("vibesys-queue", check, timeouts[0]),
            "benchmark": _command("vibesys-queue", measure, timeouts[1]) | {"result_protocol": 2},
        }
    )

    assert short == existing
    assert short.stages == existing.stages
    kinds = tuple(stage.kind for stage in short.stages)
    expected = (StageKind.CHECK, StageKind.MEASURE)
    assert kinds == (expected if build is None else (StageKind.BUILD, *expected))
    # The rendered (existing) form reads back as the same manifest.
    rendered = InputManifest.model_validate(tomllib.loads(render_input_manifest(short)))
    assert rendered == short


@pytest.mark.parametrize(("short", "existing"), [("check", "accuracy"), ("measure", "benchmark")])
def test_a_stage_declared_in_both_forms_is_refused_naming_both(short: str, existing: str) -> None:
    document = _base() | {
        "accuracy": _command("vibesys-queue", [], None),
        "benchmark": _command("vibesys-queue", [], None),
        short: _command("vibesys-queue", [], None),
    }

    with pytest.raises(ValueError, match=f"{short} and {existing}"):
        InputManifest.model_validate(document)


def test_a_headline_metric_that_disagrees_with_the_scalar_result_is_refused() -> None:
    document = _base() | {
        "metric": "ops",
        "check": _command("vibesys-queue", [], None),
        "measure": _command("vibesys-queue", [], None)
        | {"result": {"json_argument": "--json", "metric": "latency"}},
    }

    with pytest.raises(ValueError, match="disagrees"):
        InputManifest.model_validate(document)


@pytest.mark.parametrize("command", [[], [""], ["/usr/bin/make"]])
def test_a_build_command_must_be_a_relative_argv(command: list[str]) -> None:
    document = _base() | {
        "build": {"command": command},
        "check": _command("vibesys-queue", [], None),
        "measure": _command("vibesys-queue", [], None),
    }

    with pytest.raises(ValueError, match=r"build\.command"):
        InputManifest.model_validate(document)


@pytest.mark.parametrize(
    ("builds", "gates", "measured"),
    [
        (True, Gates(accuracy=True, benchmark=True), ("build", "accuracy", "benchmark")),
        (True, Gates(accuracy=True, benchmark=False), ("build", "accuracy")),
        (True, Gates(accuracy=False, benchmark=False), ()),
        (False, Gates(accuracy=True, benchmark=True), ("accuracy", "benchmark")),
    ],
)
def test_a_declared_build_is_measured_first_and_only_with_a_gate(
    *, builds: bool, gates: Gates, measured: tuple[str, ...]
) -> None:
    recipe = ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="0" * 64)

    terms = terms_of(
        recipe,
        gates,
        MetricSpace(objectives=(), relative_noise=0.0),
        Measuring(stage_seconds={"build": 60.0}, queue_allowance=1.0, builds=builds),
    )

    assert terms.measured == measured
    stage_ids = tuple(stage.stage_id for stage in terms.stages)
    assert ("build" in stage_ids) is builds
    if builds:
        assert next(s for s in terms.stages if s.stage_id == "build").execution_seconds == 60.0
