"""Product code holds no model knowledge: agentshim is its only owner.

Model knowledge is a fact about a specific model or vendor: which model ids
exist or a provider runs, its default model, a model's context window or price,
or a name-prefix table that guesses any of them. agentshim owns all of it
(``ProviderProfile.models``, ``agentshim.context_window``, its pricing table)
and VibeSys reads it through ``vs_agent.api``. A copy in VibeSys goes stale
when a model ships: the hand-kept context-window table had no Claude 5 entry,
so a 1M-token model showed a 200k limit, and the hand-kept vetted list refused
``claude-opus-5-5``.

This scans every string constant (docstrings included) in the product trees,
``src/`` and each library's ``src/``, for a model-id literal or a bare model
alias. The operator still names models in configuration and flags; that is
data, not code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, python_files, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO = Path(__file__).resolve().parents[2]

#: A model id, or the start of one used as a prefix, of a vendor whose models an
#: agent CLI runs: ``claude-opus-5-5``, ``claude-``, ``gpt-6-luna``, ``o3-mini``,
#: ``gemini-3-pro-preview``, ``gemma-2``.
MODEL_ID = re.compile(
    r"(?<![\w-])(?:claude-(?:[a-z0-9]|$)|gpt-\d|gpt-oss|chatgpt-|codex-mini|o[1-9](?:-|$)|gemini-\d|gemma-)",
    re.IGNORECASE | re.MULTILINE,
)
#: A whole string that is a vendor's model alias (``--model opus``).
MODEL_ALIAS = re.compile(r"(?:opus|sonnet|haiku|fable)(?:\[1m\])?", re.IGNORECASE)

#: ``(path relative to the repository root, literal)`` sites allowed to keep a
#: model literal, each with why. Shrink-only: remove an entry when its site
#: goes, never add one to make a new literal pass; read the fact from
#: ``vs_agent.api`` instead.
ALLOWLIST: dict[tuple[str, str], str] = {}


def model_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, literal)`` of every string constant in ``tree`` that states model knowledge."""
    return [
        (node.lineno, node.value)
        for node in walk_nodes(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (MODEL_ID.search(node.value) or MODEL_ALIAS.fullmatch(node.value.strip()))
    ]


def product_python_files(root: Path) -> list[Path]:
    """Every Python file of the product trees under ``root``: ``src/`` and ``libs/*/src``."""
    trees = [root / "src", *sorted((root / "libs").glob("*/src"))]
    return [path for tree in trees if tree.is_dir() for path in python_files(tree)]


def violations(root: Path) -> list[str]:
    """``path:line: literal`` for every model literal outside :data:`ALLOWLIST`."""
    found: list[str] = []
    for path in product_python_files(root):
        relative = path.relative_to(root).as_posix()
        tree = parse_source(path.read_text(encoding="utf-8"), relative)
        found.extend(
            f"{relative}:{line}: {literal!r}"
            for line, literal in model_literals(tree)
            if (relative, literal) not in ALLOWLIST
        )
    return found


@pytest.mark.parametrize(
    "source",
    [
        "MODELS = ('claude-opus-5-5', 'gpt-6-luna')",
        "TABLE = (('claude-', 200_000), ('gemini-', 1_048_576))",
        "DEFAULT = 'gpt-5.6-sol'",
        "WINDOWS = {'o3': 200_000}",
        "x = 'o4-mini'",
        "x = 'gemma-2'",
        "ALIAS = 'sonnet'",
        "ALIAS = 'opus[1m]'",
        "HELP = f\"e.g. 'claude-opus-5' or {other}\"",
        "def f():\n    '''Use claude-haiku-4-5 for cheap turns.'''",
        "x = 'opencode/anthropic/claude-sonnet-5'",
    ],
)
def test_the_detector_finds_model_knowledge(source: str) -> None:
    assert model_literals(parse_source(source))


@pytest.mark.parametrize(
    "source",
    [
        "x = 'claude'",
        "x = 'codex'",
        "x = '.claude/skills'",
        "x = 'claude.json'",
        "x = 'CLAUDE_CONFIG_DIR'",
        "x = 'o1x'",
        "x = 'proto3-mini'",
        "x = 'llama-3-70b'",
        "x = 'a sonnet about opus'",
        "x = 'gpt'",
        "model = config.model.name",
    ],
)
def test_the_detector_ignores_other_text(source: str) -> None:
    assert not model_literals(parse_source(source))


def test_the_scan_covers_the_product_trees_only(tmp_path: Path) -> None:
    for relative in ("src/a.py", "libs/vs-x/src/vs_x/b.py", "libs/vs-x/tests/c.py", "tests/d.py"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("M = 'claude-opus-5-5'\n", encoding="utf-8")

    assert [line.split(":")[0] for line in violations(tmp_path)] == [
        "src/a.py",
        "libs/vs-x/src/vs_x/b.py",
    ]


def test_no_model_knowledge_in_product_code() -> None:
    found = violations(REPO)
    assert not found, (
        "model knowledge belongs to agentshim; read it through vs_agent.api "
        "(vetted_models, default_model, harnesses, the agent's context window) "
        "instead of naming a model in product code:\n" + "\n".join(found)
    )


def test_every_allowlisted_site_still_has_its_literal() -> None:
    found = set(violations_including_allowed(REPO))
    stale = [site for site in ALLOWLIST if site not in found]
    assert not stale, f"remove these stale ALLOWLIST entries: {stale}"


def violations_including_allowed(root: Path) -> list[tuple[str, str]]:
    """``(path, literal)`` of every model literal, allowlisted or not."""
    return [
        (relative, literal)
        for path in product_python_files(root)
        for relative in [path.relative_to(root).as_posix()]
        for _, literal in model_literals(parse_source(path.read_text(encoding="utf-8"), relative))
    ]
