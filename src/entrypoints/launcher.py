"""Route the ``vibesys`` console command to its Python entrypoint.

Light host commands run in this process. Run commands execute in a child so
terminal signals reach the run and its own teardown remains authoritative.
"""

from __future__ import annotations

import asyncio
import sys
import tomllib
from dataclasses import dataclass, field
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sim.api import InheritedStdioLauncher, LoopSignalSource, Signals

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_sim.api import ForegroundChild, ForegroundLauncher, SignalSource


@dataclass(frozen=True)
class LauncherHost:
    """Injected child process and signal boundaries."""

    children: ForegroundLauncher = field(default_factory=InheritedStdioLauncher)
    signals: SignalSource = field(default_factory=LoopSignalSource)


def source_checkout_root() -> Path | None:
    """Return the checkout owning this module, if it has valid project metadata."""
    try:
        package_file = Path(__file__).resolve()
        root = package_file.parents[2]
        if (root / "src" / "entrypoints" / "launcher.py").resolve() != package_file:
            return None
        with (root / "pyproject.toml").open("rb") as project_file:
            project = tomllib.load(project_file).get("project")
        if not isinstance(project, dict) or project.get("name") != "vibesys":
            return None
    except (IndexError, OSError, tomllib.TOMLDecodeError):
        return None
    return root


def headless_command(args: Sequence[str]) -> list[str]:
    """Return the Python child command for a run or detached server."""
    arguments = list(args)
    module = (
        "entrypoints.server"
        if "--web" in arguments or "--web-reopen" in arguments or "--detach" in arguments
        else "entrypoints.headless"
    )
    return [sys.executable, "-m", module, *arguments]


_FORWARDED_SIGNALS = (Signals.SIGTERM, Signals.SIGHUP)


def call_child(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    children: ForegroundLauncher,
    signals: SignalSource,
) -> int:
    """Run a child until it exits, forwarding terminal shutdown signals."""
    return asyncio.run(run_child(argv, env=env, children=children, signals=signals))


async def run_child(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    children: ForegroundLauncher,
    signals: SignalSource,
) -> int:
    """Run the child on an existing event loop without preempting its teardown."""
    started: list[ForegroundChild] = []

    def forward(number: Signals) -> None:
        for child in started:
            child.send_signal(number)

    def wait_for_child() -> None:
        """The terminal already delivered SIGINT to the child; keep waiting."""

    installed: list[Signals] = []
    try:
        signals.add_handler(Signals.SIGINT, wait_for_child)
        installed.append(Signals.SIGINT)
        for number in _FORWARDED_SIGNALS:
            signals.add_handler(number, partial(forward, number))
            installed.append(number)
        child = await children.start(argv, env)
        started.append(child)
        returncode = await child.wait()
    finally:
        for number in installed:
            signals.remove_handler(number)
    return 128 - returncode if returncode < 0 else returncode


_LIGHT_COMMANDS: dict[str, str] = {
    "evals": "entrypoints.evals",
    "example": "entrypoints.examples",
    "instances": "entrypoints.instances",
    "tasks": "entrypoints.tasks",
}


def main(argv: list[str] | None = None, host: LauncherHost | None = None) -> int:
    """Entry point for the ``vibesys`` console script."""
    host = LauncherHost() if host is None else host
    args = list(sys.argv[1:] if argv is None else argv)
    light = _LIGHT_COMMANDS.get(args[0]) if args else None
    if light is not None:
        command: Callable[[list[str]], int] = import_module(light).main
        return command(args[1:])
    return call_child(headless_command(args), children=host.children, signals=host.signals)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
