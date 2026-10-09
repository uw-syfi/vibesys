"""A type named ``*Sandbox*`` is agent confinement; a command runner is not one.

"Sandbox" is reserved for restricting the agent process (see
``docs/contributing/sandboxing.md``). A handle that only executes a shell
command is a ``CommandRunner``. This test keeps the word from drifting back
onto types that isolate nothing: every class whose name contains ``Sandbox`` in
product code must be listed here with the reason it is confinement, and a listed
name that no longer exists must be removed.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_CONFINEMENT = "agent confinement"
_LIFECYCLE = "lifecycle of a started confining container (DockerSandbox)"

#: Every product type with "Sandbox" in its name, and why the name is earned.
ALLOWED: dict[str, str] = {
    "WorkspaceSandbox": _CONFINEMENT,
    "HostSandbox": f"{_CONFINEMENT}: bubblewrap",
    "LandlockSandbox": f"{_CONFINEMENT}: Landlock",
    "SeatbeltSandbox": f"{_CONFINEMENT}: Seatbelt",
    "DockerSandbox": f"{_CONFINEMENT}: container, and also a command runner",
    "DockerSandboxNotStartedError": "DockerSandbox misuse",
    "SandboxUnavailableError": "required confinement is unavailable",
    "_ConfinableSandbox": "structural view of a confinement that can wrap argv",
    "_AgentPathSandbox": "structural view of a confinement that maps agent paths",
    "ProjectSandboxPaths": "project paths that feed the confinement policy",
    "SandboxKind": "selects how make_sandbox builds a runner; DOCKER builds a confinement",
    "SandboxLifecycle": _LIFECYCLE,
    "SandboxLifecycleHooks": _LIFECYCLE,
    "SandboxLifecycleError": _LIFECYCLE,
    "SandboxSession": _LIFECYCLE,
}


def sandbox_type_names(source: str) -> set[str]:
    """Return the names of classes and type aliases in *source* containing "Sandbox"."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            names.add(node.name.id)
    return {name for name in names if "Sandbox" in name}


def _product_sources() -> list[Path]:
    roots = [_REPOSITORY_ROOT / "src", *sorted((_REPOSITORY_ROOT / "libs").glob("*/src"))]
    return [path for root in roots for path in sorted(root.rglob("*.py"))]


def _declared_sandbox_types() -> dict[str, Path]:
    declared: dict[str, Path] = {}
    for path in _product_sources():
        for name in sandbox_type_names(path.read_text(encoding="utf-8")):
            declared[name] = path
    return declared


def test_scanner_finds_classes_and_aliases() -> None:
    source = "class FooSandbox: ...\ntype BarSandbox = int\nclass Plain: ...\nSandbox = 1\n"
    assert sandbox_type_names(source) == {"FooSandbox", "BarSandbox"}


def test_sandbox_in_a_type_name_means_confinement() -> None:
    unexpected = {
        name: str(path.relative_to(_REPOSITORY_ROOT))
        for name, path in _declared_sandbox_types().items()
        if name not in ALLOWED
    }
    assert not unexpected, (
        "These types put 'Sandbox' in their name. If they only run commands, name them "
        "*Runner; if they confine the agent, add them to ALLOWED with the reason: "
        f"{unexpected}"
    )


def test_allowlist_names_only_existing_types() -> None:
    stale = sorted(set(ALLOWED) - set(_declared_sandbox_types()))
    assert not stale, f"Remove these from ALLOWED, they no longer exist: {stale}"
