from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vibesys.sandbox import model_requests
from vibesys.sandbox.model_requests import (
    MODEL_MANIFEST_RELPATH,
    ModelRequest,
    ModelRequestError,
    check_allowed,
    read_model_requests,
    reconcile_model_requests,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_manifest(workspace: Path, payload: object) -> None:
    path = workspace / MODEL_MANIFEST_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


# -- read_model_requests ----------------------------------------------------


def test_missing_manifest_yields_empty(tmp_path: Path) -> None:
    assert read_model_requests(tmp_path) == []


def test_reads_bare_list(tmp_path: Path) -> None:
    _write_manifest(tmp_path, [{"id": "org/Foo-1.2-X", "revision": "abc"}])
    assert read_model_requests(tmp_path) == [ModelRequest(model_id="org/Foo-1.2-X", revision="abc")]


def test_reads_models_object_and_model_id_alias(tmp_path: Path) -> None:
    _write_manifest(tmp_path, {"models": [{"model_id": "org/bar"}]})
    assert read_model_requests(tmp_path) == [ModelRequest(model_id="org/bar", revision=None)]


def test_dedupes_keeping_first(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        [
            {"id": "org/dup", "revision": "first"},
            {"id": "org/dup", "revision": "second"},
            {"id": "org/other"},
        ],
    )
    assert read_model_requests(tmp_path) == [
        ModelRequest(model_id="org/dup", revision="first"),
        ModelRequest(model_id="org/other", revision=None),
    ]


def test_strips_whitespace_from_id(tmp_path: Path) -> None:
    _write_manifest(tmp_path, [{"id": "  org/foo  "}])
    assert read_model_requests(tmp_path) == [ModelRequest(model_id="org/foo", revision=None)]


def test_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / MODEL_MANIFEST_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with pytest.raises(ModelRequestError, match="not valid JSON"):
        read_model_requests(tmp_path)


def test_non_list_raises(tmp_path: Path) -> None:
    _write_manifest(tmp_path, {"models": "org/foo"})
    with pytest.raises(ModelRequestError, match="must be a JSON list"):
        read_model_requests(tmp_path)


def test_non_object_entry_raises(tmp_path: Path) -> None:
    _write_manifest(tmp_path, ["org/foo"])
    with pytest.raises(ModelRequestError, match="entry 0 is not an object"):
        read_model_requests(tmp_path)


def test_missing_id_raises(tmp_path: Path) -> None:
    _write_manifest(tmp_path, [{"revision": "abc"}])
    with pytest.raises(ModelRequestError, match="non-empty"):
        read_model_requests(tmp_path)


def test_non_string_revision_raises(tmp_path: Path) -> None:
    _write_manifest(tmp_path, [{"id": "org/foo", "revision": 3}])
    with pytest.raises(ModelRequestError, match="revision"):
        read_model_requests(tmp_path)


# -- check_allowed ----------------------------------------------------------


def test_allow_none_is_unrestricted() -> None:
    assert check_allowed("anything/at-all", None) is True


def test_allow_prefix_match_and_miss() -> None:
    allow = ("org/", "trusted-org/")
    assert check_allowed("org/foo", allow) is True
    assert check_allowed("trusted-org/bar", allow) is True
    assert check_allowed("other/baz", allow) is False


def test_allow_prefixes_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBESYS_MODEL_REQUEST_ALLOW", " org/ , trusted/ ")
    assert model_requests._allow_prefixes() == ("org/", "trusted/")


def test_allow_prefixes_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIBESYS_MODEL_REQUEST_ALLOW", raising=False)
    assert model_requests._allow_prefixes() is None


# -- reconcile_model_requests ----------------------------------------------


def test_reconcile_empty_does_not_touch_provisioner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []

    def _fail_if_called(*args: object, **_kwargs: object) -> str:
        called.append(args)
        return "vol"

    monkeypatch.setattr("vs_sandbox.api.ensure_model_volume", _fail_if_called)
    assert reconcile_model_requests(tmp_path) == []
    assert called == []


def test_reconcile_provisions_each_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_manifest(
        tmp_path,
        [{"id": "org/a", "revision": "r1"}, {"id": "org/b"}],
    )
    seen: list[tuple[str, str | None]] = []

    def _fake_ensure(model_id: str, *, revision: str | None = None, **_kwargs: object) -> str:
        seen.append((model_id, revision))
        return f"vibesys-model-{model_id.replace('/', '-')}"

    monkeypatch.setattr("vs_sandbox.api.ensure_model_volume", _fake_ensure)
    volumes = reconcile_model_requests(tmp_path)
    assert seen == [("org/a", "r1"), ("org/b", None)]
    assert volumes == ["vibesys-model-org-a", "vibesys-model-org-b"]


def test_reconcile_rejects_disallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_manifest(tmp_path, [{"id": "sketchy/model"}])
    monkeypatch.setenv("VIBESYS_MODEL_REQUEST_ALLOW", "trusted/")

    def _must_not_run(*_args: object, **_kwargs: object) -> str:
        pytest.fail("provisioner must not run for disallowed request")

    monkeypatch.setattr("vs_sandbox.api.ensure_model_volume", _must_not_run)
    with pytest.raises(ModelRequestError, match="not permitted"):
        reconcile_model_requests(tmp_path)
