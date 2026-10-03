from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    ModelArtifactRequest,
    PreparedModelArtifacts,
    prepare_model_artifacts,
)
from vs_sandbox.api import EnvironmentBindMount

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class _Download:
    model_id: str
    revision: str | None
    cache_dir: str


class _FakeModelDownloader:
    """Filesystem-backed fake of immutable Hugging Face snapshot downloads."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.downloads: list[_Download] = []

    def __call__(
        self,
        model_id: str,
        *,
        revision: str | None,
        cache_dir: str,
    ) -> Path:
        self.downloads.append(_Download(model_id, revision, cache_dir))
        snapshot = self._root / model_id.replace("/", "--") / (revision or "main")
        snapshot.mkdir(parents=True, exist_ok=True)
        return snapshot


def _request(
    tmp_path: Path,
    reference_dir: Path,
    *,
    runtime_artifact_dir: Path | None = None,
) -> ModelArtifactRequest:
    return ModelArtifactRequest(
        reference_dir=reference_dir,
        model_cache_dir=tmp_path / "shared" / "huggingface",
        runtime_artifact_dir=runtime_artifact_dir or reference_dir,
        log=lambda _message: None,
    )


def test_missing_reference_directory_needs_no_model_policy(tmp_path: Path) -> None:
    prepared = prepare_model_artifacts(
        _request(tmp_path, tmp_path / "missing"),
        isolated=True,
        materialize_local_weights=True,
    )

    assert prepared == PreparedModelArtifacts()


def test_authored_models_win_and_are_mounted_read_only(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    model = reference / "model"
    draft_model = reference / "draft_model"
    model.mkdir(parents=True)
    draft_model.mkdir()
    (reference / "meta.json").write_text(json.dumps({"model_id": "org/remote-model"}))
    runtime_artifacts = tmp_path / "runtime-artifacts"
    (runtime_artifacts / "model").mkdir(parents=True)
    fake = _FakeModelDownloader(tmp_path / "downloads")

    prepared = prepare_model_artifacts(
        _request(
            tmp_path,
            reference,
            runtime_artifact_dir=runtime_artifacts,
        ),
        isolated=True,
        materialize_local_weights=True,
        downloader=fake,
    )

    assert fake.downloads == []
    assert prepared == PreparedModelArtifacts(
        copy_excludes=frozenset({"model", "draft_model"}),
        bind_mounts=(
            EnvironmentBindMount(model, "/model", read_only=True),
            EnvironmentBindMount(draft_model, "/draft_model", read_only=True),
        ),
    )


def test_nonisolated_runs_keep_model_artifacts_in_workspace_copy(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    model = reference / "model"
    model.mkdir(parents=True)

    prepared = prepare_model_artifacts(
        _request(tmp_path, reference),
        isolated=False,
        materialize_local_weights=False,
    )

    assert prepared.copy_excludes == frozenset()
    assert prepared.bind_mounts == (EnvironmentBindMount(model, "/model", read_only=True),)


@pytest.mark.parametrize("materialize_local_weights", [False, True])
def test_missing_metadata_requires_an_authored_primary_model(
    tmp_path: Path,
    *,
    materialize_local_weights: bool,
) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()

    with pytest.raises(FileNotFoundError, match=r"no meta[.]json to download from"):
        prepare_model_artifacts(
            _request(tmp_path, reference),
            isolated=True,
            materialize_local_weights=materialize_local_weights,
        )


def test_remotely_provisioned_metadata_does_not_download_locally(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "meta.json").write_text(json.dumps({"model_id": "org/model"}))
    fake = _FakeModelDownloader(tmp_path / "downloads")

    prepared = prepare_model_artifacts(
        _request(tmp_path, reference),
        isolated=True,
        materialize_local_weights=False,
        downloader=fake,
    )

    assert fake.downloads == []
    assert prepared == PreparedModelArtifacts(copy_excludes=frozenset({"model", "draft_model"}))


@pytest.mark.parametrize("separate_runtime_artifacts", [False, True])
def test_download_uses_shared_cache_and_selected_runtime_artifact_path(
    tmp_path: Path,
    *,
    separate_runtime_artifacts: bool,
) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "meta.json").write_text(
        json.dumps(
            {
                "model_id": "org/model",
                "revision": "abc123",
                "architectures": ["ExampleModel"],
            }
        )
    )
    runtime_artifacts = tmp_path / "local-state" if separate_runtime_artifacts else reference
    fake = _FakeModelDownloader(tmp_path / "downloads")
    request = _request(
        tmp_path,
        reference,
        runtime_artifact_dir=runtime_artifacts,
    )

    prepared = prepare_model_artifacts(
        request,
        isolated=True,
        materialize_local_weights=True,
        downloader=fake,
    )

    model_path = runtime_artifacts / "model"
    assert fake.downloads == [_Download("org/model", "abc123", str(request.model_cache_dir))]
    assert model_path.is_symlink()
    assert model_path.resolve() == tmp_path / "downloads" / "org--model" / "abc123"
    assert prepared.bind_mounts == (EnvironmentBindMount(model_path, "/model", read_only=True),)
    if separate_runtime_artifacts:
        assert not (reference / "model").exists()


def test_broken_runtime_symlink_is_repaired_by_download(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "meta.json").write_text(json.dumps({"model_id": "org/model"}))
    runtime_artifacts = tmp_path / "local-state"
    runtime_artifacts.mkdir()
    model_path = runtime_artifacts / "model"
    model_path.symlink_to(tmp_path / "missing-snapshot")
    fake = _FakeModelDownloader(tmp_path / "downloads")

    prepare_model_artifacts(
        _request(
            tmp_path,
            reference,
            runtime_artifact_dir=runtime_artifacts,
        ),
        isolated=True,
        materialize_local_weights=True,
        downloader=fake,
    )

    assert len(fake.downloads) == 1
    assert model_path.resolve() == tmp_path / "downloads" / "org--model" / "main"


def test_valid_runtime_symlink_is_reused_without_download(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "meta.json").write_text(json.dumps({"model_id": "org/model"}))
    snapshot = tmp_path / "existing-snapshot"
    snapshot.mkdir()
    runtime_artifacts = tmp_path / "local-state"
    runtime_artifacts.mkdir()
    model_path = runtime_artifacts / "model"
    model_path.symlink_to(snapshot)
    fake = _FakeModelDownloader(tmp_path / "downloads")

    prepared = prepare_model_artifacts(
        _request(
            tmp_path,
            reference,
            runtime_artifact_dir=runtime_artifacts,
        ),
        isolated=True,
        materialize_local_weights=True,
        downloader=fake,
    )

    assert fake.downloads == []
    assert prepared.bind_mounts == (EnvironmentBindMount(model_path, "/model", read_only=True),)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"revision": "abc", "unrelated": True}, "missing required 'model_id' field"),
        ({"model_id": "org/model", "revision": 3}, "'revision' must be a string"),
    ],
)
def test_model_metadata_validates_only_required_subset(
    tmp_path: Path,
    metadata: dict[str, object],
    message: str,
) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "meta.json").write_text(json.dumps(metadata))

    with pytest.raises(ValueError, match=message):
        prepare_model_artifacts(
            _request(tmp_path, reference),
            isolated=True,
            materialize_local_weights=True,
            downloader=_FakeModelDownloader(tmp_path / "downloads"),
        )


def test_model_metadata_must_be_a_json_object(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "meta.json").write_text(json.dumps([{"model_id": "org/model"}]))

    with pytest.raises(TypeError, match="must contain a JSON object"):
        prepare_model_artifacts(
            _request(tmp_path, reference),
            isolated=True,
            materialize_local_weights=True,
            downloader=_FakeModelDownloader(tmp_path / "downloads"),
        )
