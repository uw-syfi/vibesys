"""Guard against re-scattering VibeSys's provider decisions.

``vs_agent.provider_policy`` is the one place VibeSys states which CLI
providers it ships and what it decides to do with them. Before it existed,
the same provider-name literals were hand-copied across the AgentShim launcher,
``cli_docker``, and the headless entrypoint's ``--cli-provider`` flag, and
those copies could silently drift from each other. This test scans the
source for a shipped-provider literal appearing anywhere it should instead be
a reference to ``provider_policy``, so a future edit that reintroduces one of
these literals fails here instead of drifting quietly again.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import agentshim

from vibesys.constants import PROJECT_ROOT
from vs_agent.api import SHIPPED_PROVIDERS

if TYPE_CHECKING:
    from pathlib import Path

_PROVIDER_LITERALS = frozenset({"claude", "codex", "gemini", "opencode", "copilot"})

# Every shipped Python source tree: each library's package and the product code.
_SCAN_ROOTS = (*sorted((PROJECT_ROOT / "libs").glob("*/src")), PROJECT_ROOT / "src")

# Files (or directories) exempt from the "no bare provider literal" rule, and
# why. Each entry maps a path (relative to the repository root, POSIX-style)
# to either ``None`` (every occurrence in the file is exempt) or a frozenset
# of the specific literals that are exempt there; any other literal in that
# file still fails.
_ALLOWED_LITERALS_BY_PATH: dict[str, frozenset[str] | None] = {
    # The seam that reads agentshim's own provider facts, and the module this
    # test is guarding, both are expected to name providers directly.
    "libs/vs-agent/src/vs_agent/provider_policy.py": None,
    "libs/vs-agent/src/vs_agent/provider_profiles.py": None,
    # The chat model picker's suggestion catalog is keyed by shipped provider.
    # It is a list of model names to offer, not a behavior branch.
    "src/server/chat/options.py": None,
    # Test fakes that script one provider's wire format (a Claude peer, the
    # profile a scripted session borrows, a Codex-shaped spec default). They
    # are not production behavior branches.
    "libs/vs-agent/src/vs_agent/scripted_provider.py": frozenset({"claude", "codex"}),
    "libs/vs-agent/src/vs_agent/stream_peers.py": frozenset({"claude"}),
}


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
        "vs_agent.provider_policy instead:\n" + "\n".join(violations)
    )


def test_shipped_providers_are_registered_with_agentshim() -> None:
    """VibeSys cannot ship a provider the library does not know about."""
    assert set(SHIPPED_PROVIDERS) <= set(agentshim.provider_names())
