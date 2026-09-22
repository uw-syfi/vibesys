"""The ``vibesys`` console entry point (unified launcher).

``vibesys`` is the single entry point for both installed and in-repo use, and
replaces the former ``./vs`` script. From a source checkout, run it with
``uv run vibesys``.

It routes to the headless engine, with no JavaScript runtime required, when:

* ``--headless`` is passed,
* the first argument is ``validate``, or
* stdin/stdout is not a TTY (pipes, CI).

Otherwise it starts the interactive OpenTUI client, resolving the TUI in order:

1. a prebuilt payload bundled into the wheel (``entrypoints/_tui``), run under its
   vendored Bun -- the hermetic end-user path;
2. a source checkout (``clients/tui``), built on demand with the system JS
   toolchain (Bun + Node 20+ + pnpm) -- the developer path that subsumes
   ``./vs``;
3. otherwise, a notice plus the headless engine.

The headless path runs ``python -m entrypoints.headless`` in a subprocess.
Interactive paths run the compiled launcher with ``VIBESYS_PYTHON`` set so it
drives the current interpreter.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import NoReturn

from vibesys.api import boot_trace

_MIN_NODE_MAJOR = 20

#: Files and directories whose changes trigger a source-TUI rebuild, relative to
#: the repository root. Mirrors the staleness check the old ``./vs`` script used.
#:
#: Principle: watch only inputs to the *shipped bundle* (``clients/tui/dist``),
#: not everything that happens to live nearby. On the Python side that means
#: the modules that actually shape ``ProtocolDocument`` in
#: ``server.api.schema`` -- ``events.py``, ``protocol.py``, and
#: ``diagnostics.py`` -- not all of ``src/server``. None of the other server
#: modules feed the Pydantic models serialized into the schema, so changes to
#: them cannot change the generated output and are deliberately excluded here.
_REBUILD_WATCH_FILES: tuple[str, ...] = (
    "clients/backend-client/package.json",
    "clients/backend-client/tsconfig.json",
    "clients/backend-client/tsconfig.check.json",
    "clients/core-state/package.json",
    "clients/core-state/tsconfig.json",
    "clients/core-state/tsconfig.check.json",
    "clients/tui/package.json",
    "clients/tui/tsconfig.json",
    "clients/tui/tsconfig.check.json",
    "clients/package.json",
    "clients/pnpm-lock.yaml",
    "clients/pnpm-workspace.yaml",
    "clients/biome.json",
    # Inputs to `generate:protocol` (python -m server.api.schema), which
    # feeds clients/backend-client's generated types and, downstream, the TUI
    # bundle. See the principle above for why this is the full set, no more.
    "src/server/api/schema.py",
    "src/server/events.py",
    "src/server/api/protocol.py",
    "src/server/controller.py",
    "src/server/diagnostics.py",
    "src/server/settings.py",
)
_REBUILD_WATCH_DIRS: tuple[str, ...] = (
    "clients/backend-client/src",
    "clients/core-state/src",
    "clients/tui/src",
)


@dataclass(frozen=True)
class BundledTui:
    """Paths needed to launch the wheel's self-contained interactive client."""

    root: Path
    runtime: Path
    launcher: Path


def bundled_tui() -> BundledTui | None:
    """Return the staged prebuilt TUI paths, or ``None`` when not bundled."""
    try:
        base = Path(str(files("entrypoints"))) / "_tui"
    except (ModuleNotFoundError, TypeError):  # pragma: no cover - defensive
        return None
    if not base.is_dir():
        return None
    return BundledTui(
        root=base,
        runtime=base / "bin" / "bun",
        launcher=base / "app" / "dist" / "launcher.js",
    )


def _headless_requested(args: list[str]) -> bool:
    """Whether to run the engine directly instead of the interactive TUI.

    ``--help``/``-h`` and ``validate`` never need the TUI, so they always go to
    the engine (and never trigger a source-checkout build).
    """
    if "--headless" in args or "--help" in args or "-h" in args:
        return True
    if args and args[0] in {"tui-defaults", "validate"}:
        return True
    return not (sys.stdin.isatty() and sys.stdout.isatty())


def _run_headless(args: list[str]) -> int:
    module = "entrypoints.server" if args and args[0] == "tui-defaults" else "entrypoints.headless"
    command_args = args if module == "entrypoints.server" else _without_option(args, "--theme")
    return subprocess.call(  # noqa: S603  # tracked: #288
        [sys.executable, "-m", module, *command_args]
    )


def _without_option(args: list[str], option: str) -> list[str]:
    """Remove one value-bearing launcher option from an argument vector."""
    remaining: list[str] = []
    skip_next = False
    prefix = f"{option}="
    for argument in args:
        if skip_next:
            skip_next = False
            continue
        if argument == option:
            skip_next = True
            continue
        if argument.startswith(prefix):
            continue
        remaining.append(argument)
    return remaining


# ---------------------------------------------------------------------------
# Tier 1: prebuilt payload bundled into a platform wheel
# ---------------------------------------------------------------------------


def _bundled_runtime_missing_message() -> str:
    return (
        "vibesys: the bundled Bun runtime or TUI launcher is missing or not executable.\n"
        "Reinstall the platform wheel, or run headless instead:\n"
        "  vibesys --headless --project /path/to/repository --task TASK ..."
    )


def _run_bundled_tui(bundle: BundledTui, args: list[str]) -> int:
    if not bundle.runtime.is_file() or not os.access(bundle.runtime, os.X_OK):
        print(_bundled_runtime_missing_message(), file=sys.stderr)  # noqa: T201  # tracked: #288
        return 1
    if not bundle.launcher.is_file():
        print(_bundled_runtime_missing_message(), file=sys.stderr)  # noqa: T201  # tracked: #288
        return 1

    env = {
        **os.environ,
        "BUN_CONFIG_SKIP_INSTALL_PACKAGES": "1",
        "VIBESYS_PYTHON": sys.executable,
        "VIBESYS_TUI_RUNTIME": str(bundle.runtime),
        # Launch anchor and stderr-trace request. The launcher (launcher.ts)
        # forwards its own environment to the frontend it spawns, so these
        # reach clients/tui/src/boot-trace.ts unchanged.
        **boot_trace.child_env(),
    }
    return subprocess.call(  # noqa: S603  # tracked: #288
        [str(bundle.runtime), str(bundle.launcher), *args],
        env=env,
    )


# ---------------------------------------------------------------------------
# Tier 2: build and run the TUI from a source checkout (subsumes ./vs)
# ---------------------------------------------------------------------------


def _reject_json_constant(value: str) -> NoReturn:
    message = "Invalid JSON constant"
    raise json.JSONDecodeError(message, value, 0)


def source_checkout_root() -> Path | None:
    """Return the VibeSys checkout owning this module, or ``None``."""
    try:
        package_file = Path(__file__).resolve()
        root = package_file.parents[2]
        if (root / "src" / "entrypoints" / "launcher.py").resolve() != package_file:
            return None

        with (root / "pyproject.toml").open("rb") as pyproject_file:
            pyproject = tomllib.load(pyproject_file)
        project = pyproject.get("project")
        if not isinstance(project, dict) or project.get("name") != "vibesys":
            return None

        with (root / "clients" / "tui" / "package.json").open(encoding="utf-8") as package_file:
            tui_package = json.load(package_file, parse_constant=_reject_json_constant)
        if not isinstance(tui_package, dict) or tui_package.get("name") != "@vibesys/tui":
            return None
    except (IndexError, OSError, UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        return None
    return root


def _bun_executable() -> Path | None:
    found = shutil.which("bun")
    if found is not None:
        return Path(found)
    fallback = Path.home() / ".bun" / "bin" / "bun"
    return fallback if fallback.is_file() and os.access(fallback, os.X_OK) else None


def _node_executable() -> Path | None:
    found = shutil.which("node")
    return Path(found) if found is not None else None


def _node_major(node: Path) -> int | None:
    try:
        result = subprocess.run(  # noqa: S603  # tracked: #288
            [str(node), "--version"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.match(r"v(\d+)", result.stdout.strip())
    return int(match.group(1)) if match else None


def _pnpm_argv() -> list[str] | None:
    pnpm = shutil.which("pnpm")
    if pnpm is not None:
        return [pnpm]
    corepack = shutil.which("corepack")
    if corepack is not None:
        return [corepack, "pnpm"]
    return None


def _stale_reason(root: Path) -> str | None:
    """Return why the source TUI bundle needs a rebuild, or ``None`` if fresh.

    The result is a path relative to ``root`` (or a short description for the
    missing-output case), meant for a user-facing message. It names the first
    offending input found while scanning ``_REBUILD_WATCH_FILES`` then
    ``_REBUILD_WATCH_DIRS`` in order, not necessarily the most-recently-changed
    one.
    """
    dist = root / "clients" / "tui" / "dist"
    entry = dist / "index.js"
    launcher = dist / "launcher.js"
    if not entry.is_file() or not launcher.is_file():
        return "clients/tui/dist/index.js (missing)"
    reference = entry.stat().st_mtime
    for rel in _REBUILD_WATCH_FILES:
        path = root / rel
        if path.is_file() and path.stat().st_mtime > reference:
            return rel
    for rel in _REBUILD_WATCH_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.stat().st_mtime > reference:
                return str(path.relative_to(root))
    return None


def _needs_rebuild(root: Path) -> bool:
    return _stale_reason(root) is not None


#: The pnpm workspace root, relative to the repository root. Every pnpm
#: command runs here, and `node_modules` and the lockfile live here.
_WORKSPACE_REL = "clients"

#: Marker written under the workspace `node_modules` after a successful
#: `pnpm install`, so later launches can skip reinstalling when nothing changed.
_INSTALL_STAMP_REL = "node_modules/.vibesys-install-stamp"


def _needs_install(root: Path) -> bool:
    """Whether `pnpm install --frozen-lockfile` must run before codegen/build.

    Skipped when `node_modules` already exists and the lockfile is no newer
    than the stamp file written after the last successful install.
    """
    workspace = root / _WORKSPACE_REL
    if not (workspace / "node_modules").is_dir():
        return True
    stamp = workspace / _INSTALL_STAMP_REL
    if not stamp.is_file():
        return True
    lockfile = workspace / "pnpm-lock.yaml"
    return lockfile.is_file() and lockfile.stat().st_mtime > stamp.stat().st_mtime


def _write_install_stamp(root: Path) -> None:
    stamp = root / _WORKSPACE_REL / _INSTALL_STAMP_REL
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()


def _run_pnpm_install(pnpm: list[str], root: Path) -> bool:
    print(  # noqa: T201  # tracked: #288
        "vibesys: installing JS dependencies (pnpm install --frozen-lockfile)...",
        file=sys.stderr,
    )
    started = time.monotonic()
    result = subprocess.run(  # noqa: S603  # tracked: #288
        [*pnpm, "install", "--frozen-lockfile"],
        cwd=str(root / _WORKSPACE_REL),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write("vibesys: failed to install JS dependencies:\n")
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        return False
    _write_install_stamp(root)
    elapsed = time.monotonic() - started
    print(f"vibesys: dependencies installed ({elapsed:.1f}s)", file=sys.stderr)  # noqa: T201  # tracked: #288
    return True


def _run_codegen_and_build(pnpm: list[str], root: Path) -> bool:
    steps = (
        [*pnpm, "--dir", "backend-client", "generate:protocol"],
        [*pnpm, "build:clients"],
    )
    for command in steps:
        result = subprocess.run(  # noqa: S603  # tracked: #288
            command, cwd=str(root / _WORKSPACE_REL), capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            sys.stderr.write("vibesys: failed to build the interactive client:\n")
            sys.stderr.write(result.stdout)
            sys.stderr.write(result.stderr)
            return False
    return True


def _ensure_source_tui_built(root: Path) -> bool:
    pnpm = _pnpm_argv()
    if pnpm is None:
        print(  # noqa: T201  # tracked: #288
            "vibesys: pnpm is required to build the interactive client. Install pnpm "
            "or enable Corepack, or run headless with --headless.",
            file=sys.stderr,
        )
        return False

    if _needs_install(root) and not _run_pnpm_install(pnpm, root):
        return False
    if _run_codegen_and_build(pnpm, root):
        return True

    # The build failed even though the install-skip heuristic considered
    # dependencies fresh (e.g. a partially removed node_modules). Fall back to
    # a full install and retry once before giving up.
    print(  # noqa: T201  # tracked: #288
        "vibesys: build failed; retrying after a full dependency install...",
        file=sys.stderr,
    )
    return _run_pnpm_install(pnpm, root) and _run_codegen_and_build(pnpm, root)


def _run_source_tui(root: Path, args: list[str]) -> int:
    bun = _bun_executable()
    if bun is None:
        print(  # noqa: T201  # tracked: #288
            "vibesys: Bun is required by the OpenTUI client. Install it from "
            "https://bun.sh, or run headless with --headless.",
            file=sys.stderr,
        )
        return 1
    node = _node_executable()
    if node is None or (_node_major(node) or 0) < _MIN_NODE_MAJOR:
        print(  # noqa: T201  # tracked: #288
            f"vibesys: Node.js {_MIN_NODE_MAJOR}+ is required for the interactive client, "
            "or run headless with --headless.",
            file=sys.stderr,
        )
        return 1
    if _needs_rebuild(root):
        reason = _stale_reason(root) or "clients/tui/dist/index.js (missing)"
        print(  # noqa: T201  # tracked: #288
            f"vibesys: TUI bundle is stale (changed: {reason}); rebuilding (~30-60s)...",
            file=sys.stderr,
        )
        started = time.monotonic()
        if not _ensure_source_tui_built(root):
            return 1
        elapsed = time.monotonic() - started
        print(f"vibesys: TUI bundle rebuilt ({elapsed:.1f}s)", file=sys.stderr)  # noqa: T201  # tracked: #288

    launcher = root / "clients" / "tui" / "dist" / "launcher.js"
    env = {
        **os.environ,
        "VIBESYS_PYTHON": sys.executable,
        # Ensure the launcher's Bun frontend is discoverable even when Bun only
        # lives under ~/.bun/bin.
        "PATH": os.pathsep.join([str(bun.parent), os.environ.get("PATH", "")]),
        # Launch anchor and stderr-trace request; see _run_bundled_tui.
        **boot_trace.child_env(),
    }
    return subprocess.call(  # noqa: S603  # tracked: #288
        [str(node), str(launcher), *args], env=env
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``vibesys`` console script."""
    # Anchored before any doctor check, staleness check, or rebuild, so the
    # frontend's boot trace reports wall time since the user actually ran the
    # command, including a source-checkout rebuild.
    boot_trace.mark_launch()
    args = list(sys.argv[1:] if argv is None else argv)

    if _headless_requested(args):
        return _run_headless(args)

    bundle = bundled_tui()
    if bundle is not None:
        return _run_bundled_tui(bundle, args)

    root = source_checkout_root()
    if root is not None:
        return _run_source_tui(root, args)

    print(  # noqa: T201  # tracked: #288
        "vibesys: interactive TUI is not bundled and no source checkout was found; "
        "running headless. Install a supported platform wheel to get the TUI, or "
        "pass --headless to silence this notice.",
        file=sys.stderr,
    )
    return _run_headless(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
