from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    ModelRequestError,
    create_model_request_reconciler,
)
from vs_runtime.api.testing import (
    FakeModelVolumeProvisioner,
    FakeModelVolumeRequest,
)

if TYPE_CHECKING:
    from pathlib import Path

MODEL_MANIFEST_RELPATH = ".vibesys/models.json"


def _write_manifest(workspace: Path, payload: object) -> None:
    path = workspace / MODEL_MANIFEST_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_missing_manifest_has_no_effect(tmp_path: Path) -> None:
    provisioner = FakeModelVolumeProvisioner()
    reconciler = create_model_request_reconciler(provisioner=provisioner, environment={})

    assert reconciler.reconcile(tmp_path) == ()
    assert provisioner.requests == ()


def test_reconcile_accepts_both_manifest_shapes_and_preserves_order(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        {"models": [{"model_id": "org/a", "revision": "r1"}, {"id": "org/b"}]},
    )
    provisioner = FakeModelVolumeProvisioner()
    reconciler = create_model_request_reconciler(provisioner=provisioner, environment={})

    assert reconciler.reconcile(tmp_path) == (
        "fake-model-volume-1",
        "fake-model-volume-2",
    )
    assert provisioner.requests == (
        FakeModelVolumeRequest("org/a", "r1"),
        FakeModelVolumeRequest("org/b", None),
    )


def test_reconcile_trims_ids_and_deduplicates_by_first_occurrence(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        [
            {"id": "  org/dup  ", "revision": "first"},
            {"id": "org/dup", "revision": "second"},
            {"id": "org/other"},
        ],
    )
    provisioner = FakeModelVolumeProvisioner()
    reconciler = create_model_request_reconciler(provisioner=provisioner, environment={})

    reconciler.reconcile(tmp_path)

    assert provisioner.requests == (
        FakeModelVolumeRequest("org/dup", "first"),
        FakeModelVolumeRequest("org/other", None),
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"models": "org/foo"}, "must be a JSON list"),
        (["org/foo"], "entry 0 is not an object"),
        ([{"revision": "abc"}], "needs a non-empty string"),
        ([{"id": "org/foo", "revision": 3}], 'entry 0 "revision" must be a string'),
    ],
)
def test_reconcile_rejects_malformed_manifest(
    tmp_path: Path, payload: object, message: str
) -> None:
    _write_manifest(tmp_path, payload)
    reconciler = create_model_request_reconciler(
        provisioner=FakeModelVolumeProvisioner(), environment={}
    )

    with pytest.raises(ModelRequestError, match=message):
        reconciler.reconcile(tmp_path)


def test_reconcile_rejects_invalid_json_with_manifest_path(tmp_path: Path) -> None:
    path = tmp_path / MODEL_MANIFEST_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    reconciler = create_model_request_reconciler(
        provisioner=FakeModelVolumeProvisioner(), environment={}
    )

    with pytest.raises(ModelRequestError, match=r"\.vibesys/models\.json is not valid JSON"):
        reconciler.reconcile(tmp_path)


def test_reconcile_applies_injected_operator_allowlist(tmp_path: Path) -> None:
    _write_manifest(tmp_path, [{"id": "trusted/model"}])
    provisioner = FakeModelVolumeProvisioner()
    reconciler = create_model_request_reconciler(
        provisioner=provisioner,
        environment={"VIBESYS_MODEL_REQUEST_ALLOW": " org/ , trusted/ "},
    )

    assert reconciler.reconcile(tmp_path) == ("fake-model-volume-1",)
    assert provisioner.requests == (FakeModelVolumeRequest("trusted/model", None),)


def test_reconcile_rejects_disallowed_request_before_provisioning(tmp_path: Path) -> None:
    _write_manifest(tmp_path, [{"id": "sketchy/model"}])
    provisioner = FakeModelVolumeProvisioner()
    reconciler = create_model_request_reconciler(
        provisioner=provisioner,
        environment={"VIBESYS_MODEL_REQUEST_ALLOW": "trusted/"},
    )

    with pytest.raises(
        ModelRequestError,
        match=(
            "model request 'sketchy/model' is not permitted by "
            "VIBESYS_MODEL_REQUEST_ALLOW='trusted/'"
        ),
    ):
        reconciler.reconcile(tmp_path)
    assert provisioner.requests == ()


def test_reconcile_propagates_provisioner_failure(tmp_path: Path) -> None:
    _write_manifest(tmp_path, [{"id": "org/model"}])
    provisioner = FakeModelVolumeProvisioner()
    provisioner.fail_with(RuntimeError("volume unavailable"))
    reconciler = create_model_request_reconciler(provisioner=provisioner, environment={})

    with pytest.raises(RuntimeError, match="volume unavailable"):
        reconciler.reconcile(tmp_path)
