"""Tests for the auto-provisioning of Modal Volumes holding model weights."""

from collections.abc import Callable
from typing import TypedDict
from unittest.mock import MagicMock

import pytest

from vs_sandbox.modal_model_setup import (
    _READY_SENTINEL,
    _volume_name_for,
    ensure_model_volume,
)


class TestVolumeNameFor:
    def test_sanitizes_slash_and_case(self) -> None:

        assert (
            _volume_name_for("meta-llama/Llama-3.1-8B-Instruct")
            == "vibesys-model-meta-llama-llama-3-1-8b-instruct"
        )

    def test_handles_colons_and_underscores(self) -> None:

        assert (
            _volume_name_for("openai/whisper_large-v3") == "vibesys-model-openai-whisper-large-v3"
        )


class _ModalMocks(TypedDict):
    volume: MagicMock
    app: MagicMock
    secret_from_dict: MagicMock


@pytest.fixture
def mock_modal(monkeypatch: pytest.MonkeyPatch) -> _ModalMocks:
    modal = pytest.importorskip("modal")

    fake_volume = MagicMock()
    fake_app = MagicMock()
    # `with app.run():` context manager
    run_cm = MagicMock()
    run_cm.__enter__ = MagicMock(return_value=run_cm)
    run_cm.__exit__ = MagicMock(return_value=False)
    fake_app.run.return_value = run_cm

    # app.function is a decorator — return the wrapped callable unchanged but
    # expose .remote as a MagicMock we can assert against.
    def _function_decorator(**_kwargs: object) -> Callable[[Callable[..., object]], MagicMock]:
        def wrap(_fn: Callable[..., object]) -> MagicMock:
            wrapped = MagicMock()
            wrapped.remote = MagicMock()
            return wrapped

        return wrap

    fake_app.function = _function_decorator

    monkeypatch.setattr(modal, "App", MagicMock(return_value=fake_app))
    monkeypatch.setattr(modal.Image, "debian_slim", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(modal.Volume, "from_name", MagicMock(return_value=fake_volume))
    secret_from_dict = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(modal.Secret, "from_dict", secret_from_dict)
    return {"volume": fake_volume, "app": fake_app, "secret_from_dict": secret_from_dict}


class TestEnsureModelVolume:
    def test_skips_upload_when_sentinel_present(self, mock_modal: _ModalMocks) -> None:

        # Volume reports the ready sentinel at root.
        entry = MagicMock()
        entry.path = _READY_SENTINEL
        mock_modal["volume"].listdir.return_value = [entry]

        logs: list[str] = []
        name = ensure_model_volume("meta-llama/Llama-3.1-8B-Instruct", log=logs.append)

        assert name == "vibesys-model-meta-llama-llama-3-1-8b-instruct"
        # App.run was never entered because we skipped the upload.
        mock_modal["app"].run.assert_not_called()
        assert any("ready" in line for line in logs)

    def test_triggers_upload_when_volume_empty(self, mock_modal: _ModalMocks) -> None:

        mock_modal["volume"].listdir.return_value = []

        logs: list[str] = []
        name = ensure_model_volume("openai/whisper-large-v3", log=logs.append)

        assert name == "vibesys-model-openai-whisper-large-v3"
        mock_modal["app"].run.assert_called_once()
        assert any("populating" in line for line in logs)

    def test_triggers_upload_when_listdir_raises(self, mock_modal: _ModalMocks) -> None:

        mock_modal["volume"].listdir.side_effect = RuntimeError("not found")

        name = ensure_model_volume("meta-llama/Llama-3.1-8B-Instruct", log=lambda *_: None)

        assert name.startswith("vibesys-model-")
        mock_modal["app"].run.assert_called_once()

    def test_forwards_hf_token_as_secret(self, mock_modal: _ModalMocks) -> None:

        mock_modal["volume"].listdir.return_value = []

        credential = "hf_tok"
        ensure_model_volume("x/y", hf_token=credential, log=lambda *_: None)

        mock_modal["secret_from_dict"].assert_called_once_with({"HF_TOKEN": "hf_tok"})

    def test_reads_hf_token_from_env(
        self, mock_modal: _ModalMocks, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        mock_modal["volume"].listdir.return_value = []
        monkeypatch.setenv("HF_TOKEN", "from_env")
        monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)

        ensure_model_volume("x/y", log=lambda *_: None)

        mock_modal["secret_from_dict"].assert_called_once_with({"HF_TOKEN": "from_env"})
