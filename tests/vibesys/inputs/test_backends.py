"""A task declares the compute backends it targets; the run takes its backend from them."""

from __future__ import annotations

import tomllib
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.constants import ComputeBackend
from vibesys.inputs import InputManifest, render_input_manifest

_BACKENDS = st.lists(st.sampled_from(list(ComputeBackend)), max_size=5, unique=True).map(tuple)


def _manifest(**fields: object) -> dict[str, Any]:
    return {
        "version": 1,
        "agent": {"domain": "generic"},
        "accuracy": {"command": ["check"]},
        "benchmark": {"command": ["measure"]},
        **fields,
    }


@given(backends=_BACKENDS)
def test_declared_backends_round_trip_through_the_rendered_manifest(
    backends: tuple[ComputeBackend, ...],
) -> None:
    manifest = InputManifest.model_validate(_manifest(backends=list(backends)))

    reread = InputManifest.model_validate(tomllib.loads(render_input_manifest(manifest)))

    assert reread.declared_backends == backends


def test_a_backend_is_declared_once() -> None:
    with pytest.raises(ValidationError, match="repeated: cpu"):
        InputManifest.model_validate(_manifest(backends=["cpu", "cpu"]))


def test_an_unknown_backend_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InputManifest.model_validate(_manifest(backends=["tpu"]))


_RESOURCES = {"accelerators_per_node": 1, "accelerator_backend": "rocm"}


def test_resources_declare_the_backend_when_backends_does_not() -> None:
    manifest = InputManifest.model_validate(_manifest(resources=_RESOURCES))

    assert manifest.declared_backends == (ComputeBackend.ROCM,)


def test_resources_must_name_a_backend_the_task_declares() -> None:
    with pytest.raises(ValidationError, match=r"resources\.accelerator_backend 'rocm'"):
        InputManifest.model_validate(_manifest(backends=["cuda"], resources=_RESOURCES))


def test_a_task_that_declares_nothing_has_no_backends() -> None:
    assert InputManifest.model_validate(_manifest()).declared_backends == ()
