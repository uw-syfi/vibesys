"""``_private/core.py``: finding the core binary, and the exit-code convention."""

from __future__ import annotations

import signal
import stat
from typing import TYPE_CHECKING

import pytest

from vibesys_evaluator._private import core
from vibesys_evaluator._private.sample import SIGNALS, with_signals

if TYPE_CHECKING:
    from pathlib import Path


def _fake(tmp_path: Path, script: str) -> str:
    path = tmp_path / "vibesys-evaluator"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture(autouse=True)
def _no_bundled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(core, "_BUNDLED_DIR", tmp_path / "no-bin")
    for env in core.ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)


def test_the_env_var_names_the_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = _fake(tmp_path, "cat >/dev/null; echo '{}'")
    monkeypatch.setenv(core.BIN_ENV, binary)
    assert core.find_binary() == binary


def test_the_bundled_binary_wins_over_the_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / core.BIN_NAME).write_text("")
    monkeypatch.setattr(core, "_BUNDLED_DIR", bundled)
    monkeypatch.setenv(core.BIN_ENV, "/elsewhere")
    assert core.find_binary() == str(bundled / core.BIN_NAME)


def test_path_is_the_last_resort(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake(tmp_path, "echo '{}'")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert core.find_binary() == str(tmp_path / core.BIN_NAME)


def test_the_missing_binary_message_points_at_the_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A source distribution (no ``_bin/``) fails with a message that says what to do."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(core.CoreNotFoundError, match=r"\$VIBESYS_EVALUATOR_BIN") as caught:
        core.find_binary()
    assert "go install" in str(caught.value)
    assert "not bundled" in str(caught.value)


@pytest.mark.parametrize("name", ["loadgen", "otelcapture"])
def test_loadgen_and_otelcapture_are_found_like_the_core(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(core.CoreNotFoundError, match=core.ENV_VARS[name]):
        core.find_binary(name)
    (tmp_path / name).write_text("")
    (tmp_path / name).chmod(0o755)
    assert core.find_binary(name) == str(tmp_path / name)
    monkeypatch.setenv(core.ENV_VARS[name], "/named")
    assert core.find_binary(name) == "/named"
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / name).write_text("")
    monkeypatch.setattr(core, "_BUNDLED_DIR", bundled)
    assert core.find_binary(name) == str(bundled / name)


def test_no_binary_is_a_typed_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(core.CoreNotFoundError):
        core.call("core", "version")


def test_call_sends_the_request_and_decodes_the_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The fake echoes its arguments and stdin, so both are seen to arrive.
    binary = _fake(tmp_path, 'printf \'{"args": "%s %s", "stdin": %s}\' "$1" "$2" "$(cat)"')
    monkeypatch.setenv(core.BIN_ENV, binary)
    assert core.call("k8s", "deploy", {"a": 1}) == {"args": "k8s deploy", "stdin": {"a": 1}}


def test_exit_one_is_a_domain_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = _fake(
        tmp_path, 'cat >/dev/null; echo \'{"error_kind": "bad_request", "message": "no"}\'; exit 1'
    )
    monkeypatch.setenv(core.BIN_ENV, binary)
    with pytest.raises(core.CoreDomainError) as raised:
        core.call("k8s", "deploy")
    assert raised.value.kind == "bad_request"
    assert raised.value.message == "no"
    assert raised.value.response["message"] == "no"


def test_another_exit_code_is_a_crash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(core.BIN_ENV, _fake(tmp_path, "cat >/dev/null; exit 2"))
    with pytest.raises(core.CoreCrashError) as raised:
        core.call("k8s", "deploy")
    assert raised.value.code == 2


def test_a_reply_that_is_not_json_is_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(core.BIN_ENV, _fake(tmp_path, "cat >/dev/null; echo not json"))
    with pytest.raises(core.CoreCrashError):
        core.call("k8s", "deploy")


def test_a_task_script_without_the_binary_exits_with_the_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    saved = {sig: signal.getsignal(sig) for sig in SIGNALS}

    def command() -> int:
        core.find_binary()
        return 0

    try:
        assert with_signals(command) == 1
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
    assert "VIBESYS_EVALUATOR_BIN" in capsys.readouterr().err
