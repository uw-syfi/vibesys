"""Tests for the ``vibesys`` console entry point."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import (
    SimpleNamespace,
)
from typing import Never, TypedDict
from unittest.mock import patch

import pytest

from entrypoints import launcher as cli


class _LaunchCall(TypedDict, total=False):
    """What the launcher handed to ``subprocess.call``, as captured by a fake."""

    cmd: list[str]
    cwd: Path | None
    env: dict[str, str] | None


def _make_source_checkout(
    tmp_path: Path,
    *,
    project_name: str = "vibesys",
    tui_name: str = "@vibesys/tui",
) -> Path:
    cli_file = tmp_path / "src" / "entrypoints" / "launcher.py"
    cli_file.parent.mkdir(parents=True)
    cli_file.write_text("# fixture\n")
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "{project_name}"\n')
    package_json = tmp_path / "clients" / "tui" / "package.json"
    package_json.parent.mkdir(parents=True)
    package_json.write_text(f'{{"name": "{tui_name}"}}\n')
    return cli_file


def _make_bundle(tmp_path: Path) -> cli.BundledTui:
    tui = tmp_path / "_tui"
    runtime = tui / "bin" / "bun"
    launcher = tui / "app" / "dist" / "launcher.js"
    runtime.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\n")
    runtime.chmod(0o755)
    launcher.write_text("// launcher\n")
    return cli.BundledTui(root=tui, runtime=runtime, launcher=launcher)


def _force_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `_headless_requested` return False regardless of the test's TTY."""
    monkeypatch.setattr(cli, "_headless_requested", lambda _args: False)


def test_bundled_tui_none_in_source_checkout() -> None:
    # The source tree ships no built _tui, so resolution returns None here.
    assert cli.bundled_tui() is None


def test_launcher_routes_noninteractive_commands_to_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess, "call", lambda command, **_kwargs: commands.append(command) or 0
    )
    for args in (
        ["--headless", "--input", "x"],
        ["validate", "bundle"],
        ["tui-defaults"],
        ["--help"],
        ["-h"],
    ):
        assert cli.main(args) == 0
    assert [command[3:] for command in commands] == [
        ["--headless", "--input", "x"],
        ["validate", "bundle"],
        ["tui-defaults"],
        ["--help"],
        ["-h"],
    ]


def test_launcher_routes_non_tty_commands_to_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    captured: _LaunchCall = {}
    monkeypatch.setattr(
        cli.subprocess, "call", lambda command, **_kwargs: captured.update(cmd=command) or 0
    )
    assert cli.main(["--input", "x"]) == 0
    assert captured["cmd"] == [sys.executable, "-m", "entrypoints.headless", "--input", "x"]


def test_headless_flag_runs_engine_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: _LaunchCall = {}

    def _call(cmd: list[str], **_kwargs: object) -> int:
        captured["cmd"] = cmd
        return 0

    monkeypatch.setattr(cli.subprocess, "call", _call)

    rc = cli.main(["--headless", "--input", "bundle", "--local"])

    assert rc == 0
    assert captured["cmd"] == [
        sys.executable,
        "-m",
        "entrypoints.headless",
        "--headless",
        "--input",
        "bundle",
        "--local",
    ]


def test_tui_defaults_runs_backend_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: _LaunchCall = {}

    def _call(cmd: list[str], **_kwargs: object) -> int:
        captured["cmd"] = cmd
        return 0

    monkeypatch.setattr(cli.subprocess, "call", _call)

    assert cli.main(["tui-defaults"]) == 0
    assert captured["cmd"] == [sys.executable, "-m", "entrypoints.server", "tui-defaults"]


def test_interactive_execs_launcher_with_python_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _make_bundle(tmp_path)
    _force_interactive(monkeypatch)
    monkeypatch.delenv("VIBESYS_BOOT_TRACE", raising=False)
    monkeypatch.setattr(cli, "bundled_tui", lambda: bundle)

    captured: _LaunchCall = {}

    def _call(cmd: list[str], *, env: dict[str, str] | None = None) -> int:
        captured["cmd"] = cmd
        captured["env"] = env
        return 0

    monkeypatch.setattr(cli.subprocess, "call", _call)

    with patch("shutil.which", side_effect=AssertionError("must not search system runtimes")):
        rc = cli.main(["--input", "bundle", "--local"])

    assert rc == 0
    assert captured["cmd"] == [
        str(bundle.runtime),
        str(bundle.launcher),
        "--input",
        "bundle",
        "--local",
    ]
    env = captured["env"]
    assert env is not None
    assert env["VIBESYS_PYTHON"] == sys.executable
    assert env["VIBESYS_TUI_RUNTIME"] == str(bundle.runtime)
    assert env["BUN_CONFIG_SKIP_INSTALL_PACKAGES"] == "1"
    # Read by clients/tui/src/boot-trace.ts to anchor the client's boot
    # measurements to when the user ran the command, not just when the
    # frontend process started. cli.main marks it through vibesys.boot_trace.
    launch_start_ms = int(env["VIBESYS_LAUNCH_START_MS"])
    assert abs(launch_start_ms - int(time.time() * 1000)) < 5_000
    # Quiet by default: the frontend traces only when asked to.
    assert "VIBESYS_BOOT_TRACE" not in env


def test_boot_trace_request_reaches_the_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``VIBESYS_BOOT_TRACE=1`` must reach the frontend, which traces itself."""
    bundle = _make_bundle(tmp_path)
    _force_interactive(monkeypatch)
    monkeypatch.setenv("VIBESYS_BOOT_TRACE", "1")
    monkeypatch.setattr(cli, "bundled_tui", lambda: bundle)

    captured: _LaunchCall = {}

    def _call(_cmd: list[str], env: dict[str, str] | None = None, **_kwargs: object) -> int:
        captured["env"] = env
        return 0

    monkeypatch.setattr(cli.subprocess, "call", _call)

    assert cli.main(["--input", "bundle", "--local"]) == 0
    env = captured["env"]
    assert env is not None
    assert env["VIBESYS_BOOT_TRACE"] == "1"


def test_interactive_with_non_executable_bundled_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _force_interactive(monkeypatch)
    bundle = _make_bundle(tmp_path)
    bundle.runtime.chmod(0o644)
    monkeypatch.setattr(cli, "bundled_tui", lambda: bundle)

    with patch("shutil.which", side_effect=AssertionError("must not search system runtimes")):
        assert cli.main([]) == 1
    error = capsys.readouterr().err
    assert "bundled Bun runtime" in error
    assert "--project /path/to/repository --task TASK" in error


def test_no_bundle_no_checkout_falls_back_to_headless(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: None)
    captured: _LaunchCall = {}

    def _call(cmd: list[str], **_kwargs: object) -> int:
        captured["cmd"] = cmd
        return 0

    monkeypatch.setattr(cli.subprocess, "call", _call)

    rc = cli.main(["--input", "bundle"])

    assert rc == 0
    assert captured["cmd"] == [
        sys.executable,
        "-m",
        "entrypoints.headless",
        "--input",
        "bundle",
    ]
    assert "no source checkout" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Tier 2: build and run the TUI from a source checkout (subsumes ./vs)
# ---------------------------------------------------------------------------


def test_source_checkout_root_finds_this_repo() -> None:
    root = cli.source_checkout_root()
    assert root is not None
    assert (root / "clients" / "tui" / "package.json").is_file()


def test_source_checkout_builds_and_runs_launcher_from_callers_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_interactive(monkeypatch)
    monkeypatch.delenv("VIBESYS_BOOT_TRACE", raising=False)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_bun_executable", lambda: Path("/usr/bin/bun"))
    monkeypatch.setattr(cli, "_node_executable", lambda: Path("/usr/bin/node"))
    monkeypatch.setattr(cli, "_node_major", lambda _node: 20)
    build_calls: list[bool] = []
    monkeypatch.setattr(cli, "_needs_rebuild", lambda _root: True)
    monkeypatch.setattr(
        cli, "_ensure_source_tui_built", lambda _root: build_calls.append(True) or True
    )

    captured: _LaunchCall = {}

    def _call(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> int:
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        return 0

    monkeypatch.setattr(cli.subprocess, "call", _call)

    rc = cli.main(["--input", "bundle", "--local"])

    assert rc == 0
    assert build_calls == [True]  # a stale checkout was rebuilt
    launcher = tmp_path / "clients" / "tui" / "dist" / "launcher.js"
    assert captured["cmd"] == ["/usr/bin/node", str(launcher), "--input", "bundle", "--local"]
    assert captured["cwd"] is None
    env = captured["env"]
    assert env is not None
    assert env["VIBESYS_PYTHON"] == sys.executable
    launch_start_ms = int(env["VIBESYS_LAUNCH_START_MS"])
    assert abs(launch_start_ms - int(time.time() * 1000)) < 5_000
    assert "VIBESYS_BOOT_TRACE" not in env


def test_source_checkout_skips_build_when_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_bun_executable", lambda: Path("/usr/bin/bun"))
    monkeypatch.setattr(cli, "_node_executable", lambda: Path("/usr/bin/node"))
    monkeypatch.setattr(cli, "_node_major", lambda _node: 22)
    monkeypatch.setattr(cli, "_needs_rebuild", lambda _root: False)

    message = "must not rebuild a fresh checkout"

    def _boom(_root: Path) -> Never:
        raise AssertionError(message)

    monkeypatch.setattr(cli, "_ensure_source_tui_built", _boom)
    monkeypatch.setattr(cli.subprocess, "call", lambda *_a, **_k: 0)

    assert cli.main([]) == 0


def test_source_checkout_missing_bun_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert cli.main([]) == 1
    assert "Bun is required" in capsys.readouterr().err


def test_source_checkout_old_node_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        {"bun": "/usr/bin/bun", "node": "/usr/bin/node"}.get,
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="v18.19.0\n"),
    )

    assert cli.main([]) == 1
    assert "Node.js 20+" in capsys.readouterr().err


def _make_fresh_checkout(tmp_path: Path) -> Path:
    root = _make_checkout(tmp_path)
    dist = root / "clients" / "tui" / "dist"
    src = root / "clients" / "tui" / "src" / "app.ts"
    _set_mtime(src, 1000)
    _set_mtime(dist / "index.js", 2000)
    _set_mtime(dist / "launcher.js", 2000)
    return root


def test_source_checkout_uses_bun_from_path_and_node_version_from_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_fresh_checkout(tmp_path)
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: root)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        {"bun": "/opt/bun/bin/bun", "node": "/usr/bin/node"}.get,
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="v20.3.1\n"),
    )
    captured: _LaunchCall = {}
    monkeypatch.setattr(
        cli.subprocess,
        "call",
        lambda command, **kwargs: captured.update(cmd=command, env=kwargs.get("env")) or 0,
    )

    assert cli.main([]) == 0
    assert captured["cmd"] == [
        "/usr/bin/node",
        str(root / "clients" / "tui" / "dist" / "launcher.js"),
    ]
    assert captured["env"] is not None
    assert captured["env"]["PATH"].split(os.pathsep)[0] == "/opt/bun/bin"


def test_source_checkout_uses_bun_home_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_fresh_checkout(tmp_path / "checkout")
    bun = tmp_path / "home" / ".bun" / "bin" / "bun"
    bun.parent.mkdir(parents=True)
    bun.write_text("#!/bin/sh\n")
    bun.chmod(0o755)

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: root)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="v22.0.0\n"),
    )

    captured: _LaunchCall = {}
    monkeypatch.setattr(
        cli.subprocess,
        "call",
        lambda command, **kwargs: captured.update(cmd=command, env=kwargs.get("env")) or 0,
    )

    assert cli.main([]) == 0
    assert captured["env"] is not None
    assert captured["env"]["PATH"].split(os.pathsep)[0] == str(bun.parent)


@pytest.mark.parametrize("failure", ["malformed", "execution"])
def test_source_checkout_rejects_unusable_node_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        {"bun": "/usr/bin/bun", "node": "/usr/bin/node"}.get,
    )

    def run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        if failure == "execution":
            message = "node could not execute"
            raise OSError(message)
        return SimpleNamespace(stdout="not-a-version\n")

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.main([]) == 1
    assert "Node.js 20+" in capsys.readouterr().err


def _make_checkout(tmp_path: Path) -> Path:
    dist = tmp_path / "clients" / "tui" / "dist"
    src = tmp_path / "clients" / "tui" / "src"
    dist.mkdir(parents=True)
    src.mkdir(parents=True)
    (dist / "index.js").write_text("// index\n")
    (dist / "launcher.js").write_text("// launcher\n")
    (src / "app.ts").write_text("// src\n")
    return tmp_path


def _make_stale_checkout(tmp_path: Path) -> Path:
    root = _make_checkout(tmp_path)
    dist = root / "clients" / "tui" / "dist"
    _set_mtime(dist / "index.js", 1000)
    _set_mtime(dist / "launcher.js", 1000)
    _set_mtime(root / "clients" / "tui" / "src" / "app.ts", 2000)
    return root


def _set_mtime(path: Path, when: float) -> None:

    os.utime(path, (when, when))


def _configure_source_checkout(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    """Configure an interactive source-checkout launch through ``main``."""
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: root)
    monkeypatch.setattr(cli, "_bun_executable", lambda: Path("/usr/bin/bun"))
    monkeypatch.setattr(cli, "_node_executable", lambda: Path("/usr/bin/node"))
    monkeypatch.setattr(cli, "_node_major", lambda _node: 20)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess,
        "call",
        lambda command, **_kwargs: calls.append(command) or 0,
    )
    return calls


def _launch_source_checkout(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, list[list[str]], list[Path]]:
    """Exercise source TUI freshness through the public launcher entry point."""
    calls = _configure_source_checkout(root, monkeypatch)
    builds: list[Path] = []

    def build_source_tui(build_root: Path) -> bool:
        builds.append(build_root)
        return True

    monkeypatch.setattr(cli, "_ensure_source_tui_built", build_source_tui)
    return cli.main([]), calls, builds


def test_source_launcher_rebuilds_stale_bundle_but_skips_fresh_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_checkout(tmp_path)
    dist = root / "clients" / "tui" / "dist"
    src_file = root / "clients" / "tui" / "src" / "app.ts"

    # Fresh: dist newer than sources -> no rebuild.
    _set_mtime(src_file, 1000)
    _set_mtime(dist / "index.js", 2000)
    _set_mtime(dist / "launcher.js", 2000)
    result, calls, builds = _launch_source_checkout(root, monkeypatch)
    assert result == 0
    assert builds == []
    assert "launcher.js" in calls[0][1]

    # Stale: a source is newer than the built entry -> rebuild.
    _set_mtime(src_file, 3000)
    result, calls, builds = _launch_source_checkout(root, monkeypatch)
    assert result == 0
    assert builds == [root]
    assert "launcher.js" in calls[0][1]

    # Missing build output -> rebuild.
    (dist / "index.js").unlink()
    result, calls, builds = _launch_source_checkout(root, monkeypatch)
    assert result == 0
    assert builds == [root]
    assert "launcher.js" in calls[0][1]


def test_ensure_built_requires_pnpm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_stale_checkout(tmp_path)
    _configure_source_checkout(root, monkeypatch)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    assert cli.main([]) == 1
    assert "pnpm is required" in capsys.readouterr().err


def test_source_launcher_installs_and_runs_pnpm_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _make_stale_checkout(tmp_path)
    launcher_calls = _configure_source_checkout(root, monkeypatch)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/pnpm" if name == "pnpm" else None
    )
    calls: list[list[str]] = []
    cwds: list[str | None] = []

    def _run(
        cmd: list[str],
        cwd: str | None = None,
        **_kwargs: object,
    ) -> SimpleNamespace:
        calls.append(cmd)
        cwds.append(cwd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", _run)
    assert cli.main([]) == 0
    assert [c[1] for c in calls] == ["install", "--dir", "build:clients"]
    assert calls[1][2:] == ["backend-client", "generate:protocol"]

    assert set(cwds) == {str(root / "clients")}
    assert (root / "clients" / "node_modules" / ".vibesys-install-stamp").is_file()
    assert launcher_calls
    assert "launcher.js" in launcher_calls[0][1]
    assert "installing JS dependencies" in capsys.readouterr().err


def test_ensure_built_reports_install_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_stale_checkout(tmp_path)
    _configure_source_checkout(root, monkeypatch)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/pnpm" if name == "pnpm" else None
    )

    def _run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="boom-out", stderr="boom-err")

    monkeypatch.setattr(cli.subprocess, "run", _run)
    assert cli.main([]) == 1
    err = capsys.readouterr().err
    assert "failed to install" in err
    assert "boom-err" in err


def test_ensure_built_skips_install_when_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _make_stale_checkout(tmp_path)
    (root / "clients" / "node_modules").mkdir()
    (root / "clients" / "node_modules" / ".vibesys-install-stamp").touch()
    _configure_source_checkout(root, monkeypatch)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/pnpm" if name == "pnpm" else None
    )
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", _run)
    assert cli.main([]) == 0
    # No "install" call: codegen/build run directly.
    assert [c[1] for c in calls] == ["--dir", "build:clients"]


def test_ensure_built_retries_with_install_after_build_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _make_stale_checkout(tmp_path)
    (root / "clients" / "node_modules").mkdir()
    (root / "clients" / "node_modules" / ".vibesys-install-stamp").touch()
    _configure_source_checkout(root, monkeypatch)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/pnpm" if name == "pnpm" else None
    )
    calls: list[list[str]] = []
    state = {"failed_once": False}

    def _run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        if cmd[-1] == "generate:protocol" and not state["failed_once"]:
            state["failed_once"] = True
            return SimpleNamespace(returncode=1, stdout="", stderr="stale-deps")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", _run)
    assert cli.main([]) == 0
    # generate:protocol failed once -> forced install -> generate:protocol and
    # build:clients both retried and succeeded.
    assert [c[1] for c in calls] == ["--dir", "install", "--dir", "build:clients"]


def test_ensure_built_reports_build_failure_after_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_stale_checkout(tmp_path)
    (root / "clients" / "node_modules").mkdir()
    (root / "clients" / "node_modules" / ".vibesys-install-stamp").touch()
    _configure_source_checkout(root, monkeypatch)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/pnpm" if name == "pnpm" else None
    )

    def _run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if cmd[-1] == "generate:protocol":
            return SimpleNamespace(returncode=1, stdout="proto-out", stderr="proto-err")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", _run)
    assert cli.main([]) == 1
    err = capsys.readouterr().err
    assert "retrying after a full dependency install" in err
    assert "failed to build" in err
    assert "proto-err" in err


def test_source_launcher_installs_when_stamp_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_stale_checkout(tmp_path)
    (root / "clients" / "node_modules").mkdir()
    _configure_source_checkout(root, monkeypatch)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/pnpm" if name == "pnpm" else None
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **_kwargs: (
            calls.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    assert cli.main([]) == 0
    assert [command[1] for command in calls] == ["install", "--dir", "build:clients"]


def test_source_launcher_installs_when_lockfile_is_newer_than_stamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_stale_checkout(tmp_path)
    workspace = root / "clients"
    (workspace / "node_modules").mkdir()
    stamp = workspace / "node_modules" / ".vibesys-install-stamp"
    stamp.touch()
    lockfile = workspace / "pnpm-lock.yaml"
    lockfile.write_text("lockfileVersion: 9\n")
    _set_mtime(stamp, 1000)
    _set_mtime(lockfile, 2000)
    _configure_source_checkout(root, monkeypatch)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/pnpm" if name == "pnpm" else None
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **_kwargs: (
            calls.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    assert cli.main([]) == 0
    assert [command[1] for command in calls] == ["install", "--dir", "build:clients"]


def test_bundled_tui_missing_launcher_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_interactive(monkeypatch)
    bundle = _make_bundle(tmp_path)
    bundle.launcher.unlink()  # runtime present + executable, launcher gone
    monkeypatch.setattr(cli, "bundled_tui", lambda: bundle)

    with patch("shutil.which", side_effect=AssertionError("must not search system runtimes")):
        assert cli.main([]) == 1
    assert "bundled Bun runtime" in capsys.readouterr().err


def test_bundled_tui_resolves_when_staged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "_tui").mkdir()
    requested_packages: list[str] = []
    monkeypatch.setattr(
        cli, "files", lambda package: requested_packages.append(package) or tmp_path
    )
    bundle = cli.bundled_tui()
    assert bundle is not None
    assert requested_packages == ["entrypoints"]
    assert bundle.root == tmp_path / "_tui"
    assert bundle.runtime == tmp_path / "_tui" / "bin" / "bun"
    assert bundle.launcher == tmp_path / "_tui" / "app" / "dist" / "launcher.js"


def test_source_checkout_root_ignores_unrelated_current_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lookalike = tmp_path / "lookalike"
    _make_source_checkout(lookalike)
    nested = lookalike / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setattr(
        cli,
        "__file__",
        tmp_path / "site-packages" / "entrypoints" / "launcher.py",
    )

    assert cli.source_checkout_root() is None


def test_source_checkout_root_rejects_wrong_python_project_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli_file = _make_source_checkout(tmp_path, project_name="not-vibesys")
    monkeypatch.setattr(cli, "__file__", cli_file)

    assert cli.source_checkout_root() is None


def test_source_checkout_root_rejects_nonstandard_json_constant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli_file = _make_source_checkout(tmp_path)
    package_json = tmp_path / "clients" / "tui" / "package.json"
    package_json.write_text('{"name":"@vibesys/tui","x":NaN}\n')
    monkeypatch.setattr(cli, "__file__", cli_file)

    assert cli.source_checkout_root() is None


def test_source_launcher_rebuilds_on_watched_config_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_checkout(tmp_path)
    dist = root / "clients" / "tui" / "dist"
    pkg = root / "clients" / "tui" / "package.json"
    pkg.write_text("{}\n")
    _set_mtime(root / "clients" / "tui" / "src" / "app.ts", 1000)
    _set_mtime(dist / "index.js", 2000)
    _set_mtime(dist / "launcher.js", 2000)
    _set_mtime(pkg, 3000)  # a watched config file newer than the build
    result, calls, builds = _launch_source_checkout(root, monkeypatch)
    assert result == 0
    assert builds == [root]
    assert "launcher.js" in calls[0][1]


def test_source_launcher_rebuilds_on_core_state_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_checkout(tmp_path)
    dist = root / "clients" / "tui" / "dist"
    core_source = root / "clients" / "core-state" / "src" / "index.ts"
    core_source.parent.mkdir(parents=True)
    core_source.write_text("export {};\n")
    _set_mtime(root / "clients" / "tui" / "src" / "app.ts", 1000)
    _set_mtime(dist / "index.js", 2000)
    _set_mtime(dist / "launcher.js", 2000)
    _set_mtime(core_source, 3000)

    result, calls, builds = _launch_source_checkout(root, monkeypatch)
    assert result == 0
    assert builds == [root]
    assert "launcher.js" in calls[0][1]


def test_source_launcher_ignores_unrelated_backend_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # src/server no longer feeds `generate:protocol` in its entirety;
    # a change to an unrelated module in that package must not trigger a
    # rebuild.
    root = _make_checkout(tmp_path)
    dist = root / "clients" / "tui" / "dist"
    unrelated = root / "src" / "server" / "inspector.py"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("# inspector\n")
    _set_mtime(root / "clients" / "tui" / "src" / "app.ts", 1000)
    _set_mtime(dist / "index.js", 2000)
    _set_mtime(dist / "launcher.js", 2000)
    _set_mtime(unrelated, 3000)

    result, calls, builds = _launch_source_checkout(root, monkeypatch)
    assert result == 0
    assert builds == []
    assert "launcher.js" in calls[0][1]


def test_source_launcher_rebuilds_on_protocol_codegen_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # protocol.py directly shapes the generated JSON schema, so it must still
    # trigger a rebuild even though src/server is no longer watched
    # wholesale.
    root = _make_checkout(tmp_path)
    dist = root / "clients" / "tui" / "dist"
    protocol = root / "src" / "server" / "api" / "protocol.py"
    protocol.parent.mkdir(parents=True)
    protocol.write_text("# protocol\n")
    _set_mtime(root / "clients" / "tui" / "src" / "app.ts", 1000)
    _set_mtime(dist / "index.js", 2000)
    _set_mtime(dist / "launcher.js", 2000)
    _set_mtime(protocol, 3000)

    result, calls, builds = _launch_source_checkout(root, monkeypatch)
    assert result == 0
    assert builds == [root]
    assert "launcher.js" in calls[0][1]


def test_source_launcher_rebuilds_on_client_settings_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_checkout(tmp_path)
    dist = root / "clients" / "tui" / "dist"
    settings = root / "src" / "server" / "settings.py"
    settings.parent.mkdir(parents=True)
    settings.write_text("# client settings\n")
    _set_mtime(root / "clients" / "tui" / "src" / "app.ts", 1000)
    _set_mtime(dist / "index.js", 2000)
    _set_mtime(dist / "launcher.js", 2000)
    _set_mtime(settings, 3000)

    result, calls, builds = _launch_source_checkout(root, monkeypatch)
    assert result == 0
    assert builds == [root]
    assert "launcher.js" in calls[0][1]


def test_source_launcher_reports_first_offending_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _make_checkout(tmp_path)
    dist = root / "clients" / "tui" / "dist"
    protocol = root / "src" / "server" / "api" / "protocol.py"
    protocol.parent.mkdir(parents=True)
    protocol.write_text("# protocol\n")
    _set_mtime(root / "clients" / "tui" / "src" / "app.ts", 1000)
    _set_mtime(dist / "index.js", 2000)
    _set_mtime(dist / "launcher.js", 2000)
    _set_mtime(protocol, 3000)

    result, _calls, builds = _launch_source_checkout(root, monkeypatch)
    assert result == 0
    assert builds == [root]
    assert "changed: src/server/api/protocol.py" in capsys.readouterr().err


def test_source_launcher_does_not_report_staleness_when_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _make_checkout(tmp_path)
    dist = root / "clients" / "tui" / "dist"
    _set_mtime(root / "clients" / "tui" / "src" / "app.ts", 1000)
    _set_mtime(dist / "index.js", 2000)
    _set_mtime(dist / "launcher.js", 2000)

    result, _calls, builds = _launch_source_checkout(root, monkeypatch)
    assert result == 0
    assert builds == []
    assert "TUI bundle is stale" not in capsys.readouterr().err


def test_run_source_tui_prints_stale_and_rebuilt_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "_bun_executable", lambda: Path("/usr/bin/bun"))
    monkeypatch.setattr(cli, "_node_executable", lambda: Path("/usr/bin/node"))
    monkeypatch.setattr(cli, "_node_major", lambda _node: 20)
    monkeypatch.setattr(cli, "_needs_rebuild", lambda _root: True)
    monkeypatch.setattr(cli, "_stale_reason", lambda _root: "src/server/api/protocol.py")
    monkeypatch.setattr(cli, "_ensure_source_tui_built", lambda _root: True)
    monkeypatch.setattr(cli.subprocess, "call", lambda *_a, **_k: 0)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: tmp_path)

    rc = cli.main([])

    assert rc == 0
    err = capsys.readouterr().err
    assert "TUI bundle is stale (changed: src/server/api/protocol.py); rebuilding" in err
    assert "TUI bundle rebuilt (" in err


def test_run_source_tui_skips_message_when_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "_bun_executable", lambda: Path("/usr/bin/bun"))
    monkeypatch.setattr(cli, "_node_executable", lambda: Path("/usr/bin/node"))
    monkeypatch.setattr(cli, "_node_major", lambda _node: 20)
    monkeypatch.setattr(cli, "_needs_rebuild", lambda _root: False)

    message = "must not compute a stale reason for a fresh checkout"

    def _boom(_root: Path) -> Never:
        raise AssertionError(message)

    monkeypatch.setattr(cli, "_stale_reason", _boom)
    monkeypatch.setattr(cli.subprocess, "call", lambda *_a, **_k: 0)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: tmp_path)

    rc = cli.main([])

    assert rc == 0
    assert capsys.readouterr().err == ""


def test_source_checkout_build_failure_returns_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_interactive(monkeypatch)
    monkeypatch.setattr(cli, "bundled_tui", lambda: None)
    monkeypatch.setattr(cli, "source_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_bun_executable", lambda: Path("/usr/bin/bun"))
    monkeypatch.setattr(cli, "_node_executable", lambda: Path("/usr/bin/node"))
    monkeypatch.setattr(cli, "_node_major", lambda _node: 20)
    monkeypatch.setattr(cli, "_needs_rebuild", lambda _root: True)
    monkeypatch.setattr(cli, "_ensure_source_tui_built", lambda _root: False)

    assert cli.main([]) == 1
