"""Guard against re-scattering VibeSys's provider decisions.

``vibesys.agents.provider_policy`` is the one place VibeSys states which CLI
providers it ships and what it decides to do with them. Before it existed,
the same provider-name literals were hand-copied across the AgentShim driver,
``cli_docker``, and the headless entrypoint's ``--cli-provider`` flag, and
those copies could silently drift from each other. This test scans the
source for a shipped-provider literal appearing anywhere it should instead be
a reference to ``provider_policy`` (or, for the omnigent integration, to
Omnigent's own provider registry), so a future edit that reintroduces one of
these literals fails here instead of drifting quietly again.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import agentshim

from vibesys.agents.provider_policy import SHIPPED_PROVIDERS
from vibesys.constants import PROJECT_ROOT

if TYPE_CHECKING:
    from pathlib import Path

_PROVIDER_LITERALS = frozenset({"claude", "codex", "gemini", "opencode", "copilot"})

_SCAN_ROOTS = (
    PROJECT_ROOT / "src" / "vibesys" / "agents",
    PROJECT_ROOT / "src" / "entrypoints",
)

# Files (or directories) exempt from the "no bare provider literal" rule, and
# why. Each entry maps a path (relative to the repository root, POSIX-style)
# to either ``None`` (every occurrence in the file is exempt) or a frozenset
# of the specific literals that are exempt there; any other literal in that
# file still fails.
_ALLOWED_LITERALS_BY_PATH: dict[str, frozenset[str] | None] = {
    # The seam that reads agentshim's own provider facts, and the module this
    # test is guarding, both are expected to name providers directly.
    "src/vibesys/agents/provider_policy.py": None,
    "src/vibesys/agents/provider_profiles.py": None,
    # Omnigent 0.10 supports exactly claude and codex and exposes no
    # provider-name abstraction of its own; this driver and its package
    # branch on Omnigent's own per-provider attributes (its executor
    # registry, its MCP translation), not on a VibeSys provider decision, so
    # routing them through vibesys.agents.provider_policy would misstate
    # ownership.
    "src/vibesys/agents/drivers/omnigent.py": None,
    # docker_executor.py: the Codex rollout watchdog recognizes a resumed
    # `codex exec --json` process and rollout file by name. It is documented
    # provider-behaviour compensation that "stays in VibeSys until the
    # behaviour is verified fixed upstream" (docs/contributing/agent-drivers.md).
    "src/vibesys/agents/docker_executor.py": frozenset({"codex"}),
}

_ALLOWED_DIR_PREFIXES = ("src/vibesys/agents/omnigent/",)


def _relative_posix(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        files.extend(sorted(root.rglob("*.py")))
    return files


def _string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _violations() -> list[str]:
    problems: list[str] = []
    for path in _iter_python_files():
        rel = _relative_posix(path)
        if any(rel.startswith(prefix) for prefix in _ALLOWED_DIR_PREFIXES):
            continue
        allowed = _ALLOWED_LITERALS_BY_PATH.get(rel, frozenset())
        if allowed is None:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for lineno, value in _string_constants(tree):
            if value not in _PROVIDER_LITERALS:
                continue
            if value in allowed:
                continue
            problems.append(f"{rel}:{lineno}: bare provider literal {value!r}")
    return problems


def test_no_bare_provider_literals_outside_the_allowlist() -> None:
    """Every shipped-provider string outside the allowlist must route through
    ``provider_policy`` (a constant, a predicate, or a profile lookup)."""
    violations = _violations()
    assert not violations, (
        "found provider-name literal(s) that should route through "
        "vibesys.agents.provider_policy instead:\n" + "\n".join(violations)
    )


def test_shipped_providers_are_registered_with_agentshim() -> None:
    """VibeSys cannot ship a provider the library does not know about."""
    assert set(SHIPPED_PROVIDERS) <= set(agentshim.provider_names())
