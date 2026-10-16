"""Product code holds no model knowledge: agentshim is its only owner.

Model knowledge is a fact about a specific model or vendor: which model ids
exist or a provider runs, its default model, a model's context window or price,
or a name-prefix table that guesses any of them. agentshim owns all of it
(``ProviderProfile.models``, ``agentshim.context_window``, its pricing table)
and VibeSys reads it through ``vs_agent.api``. A copy in VibeSys goes stale
when a model ships: the hand-kept context-window table had no Claude 5 entry,
so a 1M-token model showed a 200k limit, and the hand-kept vetted list refused
``claude-opus-5-5``.

This scans every string constant (docstrings included) in the Python product
trees, ``src/`` and each library's ``src/``, for a model-id literal or a bare
model alias, and the text of every TypeScript client source (``clients/**/src``,
tests and generated code excluded) for a model-id literal. The operator still
names models in configuration and flags; that is data, not code.
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
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "clients/tui/src/agent-runtime-label.ts",
        "gpt-5",
    ): "a doc-comment example of how a model id the backend sends is displayed; no fact is read from it",
}

#: TypeScript files under a client ``src/`` that are not product code.
_TS_NON_PRODUCT = re.compile(r"(\.test\.tsx?$|/testing/|/__tests__/|/generated/|/fixtures/)")


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


def client_typescript_files(root: Path) -> list[Path]:
    """Every TypeScript product file of the clients under ``root``: ``clients/**/src``, no tests."""
    clients = root / "clients"
    if not clients.is_dir():
        return []
    return [
        path
        for pattern in ("**/src/**/*.ts", "**/src/**/*.tsx")
        for path in sorted(clients.glob(pattern))
        if "node_modules" not in path.parts
        and not _TS_NON_PRODUCT.search(path.relative_to(root).as_posix())
    ]


def typescript_model_literals(text: str) -> list[tuple[int, str]]:
    """``(line, match)`` of every model id in TypeScript source ``text``."""
    return [
        (line, match.group(0))
        for line, source_line in enumerate(text.splitlines(), start=1)
        for match in MODEL_ID.finditer(source_line)
    ]


def all_model_literals(root: Path) -> list[tuple[str, int, str]]:
    """``(path, line, literal)`` of every model literal in the product trees, allowlisted or not."""
    found: list[tuple[str, int, str]] = []
    for path in product_python_files(root):
        relative = path.relative_to(root).as_posix()
        tree = parse_source(path.read_text(encoding="utf-8"), relative)
        found.extend((relative, line, literal) for line, literal in model_literals(tree))
    for path in client_typescript_files(root):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        found.extend((relative, line, literal) for line, literal in typescript_model_literals(text))
    return found


def violations(root: Path) -> list[str]:
    """``path:line: literal`` for every model literal outside :data:`ALLOWLIST`."""
    return [
        f"{relative}:{line}: {literal!r}"
        for relative, line, literal in all_model_literals(root)
        if (relative, literal) not in ALLOWLIST
    ]


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
    planted = (
        "src/a.py",
        "libs/vs-x/src/vs_x/b.py",
        "libs/vs-x/tests/c.py",
        "tests/d.py",
        "clients/libs/x/src/e.ts",
        "clients/features/y/src/ui/f.tsx",
        "clients/libs/x/src/e.test.ts",
        "clients/libs/x/src/testing/fake.ts",
        "clients/libs/x/src/generated/g.ts",
        "clients/libs/x/node_modules/z/src/h.ts",
        "clients/tui/dev/fixtures/i.ts",
    )
    for relative in planted:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("M = 'claude-opus-5-5'\n", encoding="utf-8")

    assert sorted(line.split(":")[0] for line in violations(tmp_path)) == [
        "clients/features/y/src/ui/f.tsx",
        "clients/libs/x/src/e.ts",
        "libs/vs-x/src/vs_x/b.py",
        "src/a.py",
    ]


def test_no_model_knowledge_in_product_code() -> None:
    found = violations(REPO)
    assert not found, (
        "model knowledge belongs to agentshim; read it through vs_agent.api "
        "(vetted_models, default_model, harnesses, the agent's context window) "
        "instead of naming a model in product code:\n" + "\n".join(found)
    )


def test_every_allowlisted_site_still_has_its_literal() -> None:
    found = {(relative, literal) for relative, _, literal in all_model_literals(REPO)}
    stale = [site for site in ALLOWLIST if site not in found]
    assert not stale, f"remove these stale ALLOWLIST entries: {stale}"
