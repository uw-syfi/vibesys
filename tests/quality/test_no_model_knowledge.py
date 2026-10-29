"""Product code holds no model knowledge: agentshim is its only owner.

Model knowledge is a fact about a specific model or vendor: which model ids
exist or a provider runs, its default model, a model's context window or price,
or a name-prefix table that guesses any of them. agentshim owns all of it
(``ProviderProfile.models``, ``agentshim.context_window``, its pricing table)
and VibeSys reads it through ``vs_agent.api``. A copy in VibeSys goes stale
when a model ships: the hand-kept context-window table had no Claude 5 entry,
so a 1M-token model showed a 200k limit, and the hand-kept vetted list refused
``claude-opus-5-5``.

The scan reads three kinds of product file:

- Python in ``src/`` and each library's ``src/``: every string constant
  (docstrings included) and every ``+`` concatenation of string constants is
  matched against a model id or a model alias (``opus``, ``sonnet-5``,
  ``Opus 5.5``), and a dict or tuple-of-pairs literal mapping a vendor or
  provider name to a token count (``{'anthropic': 200_000}``) is a context
  table.
- Every other text file in those trees (prompt templates, bundled TOML, JSON,
  YAML, Markdown, shell), matched line by line for a model id.
- Every TypeScript and JavaScript file under ``clients/``, matched line by
  line for a model id. Excluded: tests, generated code, test fixtures and
  fakes, and installed or built output.

Not scanned: ``tests/`` (a test names models to pin behavior), ``docs/`` and
``examples/`` (an operator's configuration names models, which is data), and
lock or package manifests. The operator still names models in configuration
and flags; that is data, not code.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, python_files, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO = Path(__file__).resolve().parents[2]

#: A model id, or the start of one used as a prefix, of a vendor whose models an
#: agent CLI runs: ``claude-opus-5-5``, ``claude-``, ``gpt-6-luna``, ``o3-mini``,
#: ``gemini-3-pro-preview``, ``gemma-2``.
#: ``claude-code``, Claude Code's npm package and CLI name, is not a model.
MODEL_ID = re.compile(
    r"(?<![\w-])(?:claude-(?!code\b)(?:[a-z0-9]|$)|gpt-\d|gpt-oss|chatgpt-|codex-mini|o[1-9](?:-|$)"
    r"|gemini-\d|gemma-)",
    re.IGNORECASE | re.MULTILINE,
)
#: A whole string that is a vendor's model alias or family name, optionally
#: versioned: ``opus``, ``opus[1m]``, ``sonnet-5``, ``opus-5-5``, ``Opus 5.5``.
MODEL_ALIAS = re.compile(
    r"(?:opus|sonnet|haiku|fable)(?:[-\s]?\d[\w.-]*)?(?:\[1m\])?", re.IGNORECASE
)
#: Names a context or price table is keyed by: a vendor or a provider CLI.
VENDOR_KEYS = frozenset({"anthropic", "openai", "google", "claude", "codex", "gemini"})
#: The smallest value that reads as a token count rather than a small setting.
MIN_TOKEN_COUNT = 8192

#: ``(path relative to the repository root, literal)`` sites allowed to keep a
#: model literal, each with why. Shrink-only: remove an entry when its site
#: goes, never add one to make a new literal pass; read the fact from
#: ``vs_agent.api`` instead.
ALLOWLIST: dict[tuple[str, str], str] = {}

#: Directories under ``clients/`` holding installed or built output, never walked.
_SKIPPED_DIRS = frozenset({"node_modules", "dist", "build", "coverage", ".turbo"})
#: A client source file by extension.
_CLIENT_CODE = re.compile(r"\.(?:ts|tsx|mts|cts|js|jsx|mjs|cjs)$")
#: Client files that are not product code: tests, generated code, fixtures and
#: fakes, and installed or built output.
_TS_NON_PRODUCT = re.compile(
    r"(\.(?:test|spec)\.[cm]?[jt]sx?$|/testing/|/__tests__/|/tests?/|/e2e/|/generated/|/fixtures/"
    r")"
)


def _folded_str(node: ast.AST) -> str | None:
    """The value of a string constant, or of a ``+`` chain of them, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded_str(node.left), _folded_str(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _states_a_model(text: str) -> bool:
    return bool(MODEL_ID.search(text) or MODEL_ALIAS.fullmatch(text.strip()))


def _vendor_count(key: ast.AST | None, value: ast.AST) -> str | None:
    """``vendor: count`` when ``key`` names a vendor and ``value`` is a token count."""
    if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
        return None
    if key.value.strip().lower() not in VENDOR_KEYS:
        return None
    count = value.value if isinstance(value, ast.Constant) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < MIN_TOKEN_COUNT:
        return None
    return f"{key.value}: {count}"


def _vendor_table_rows(node: ast.Dict | ast.Tuple | ast.List) -> list[str]:
    """Each ``vendor: count`` row of a dict or tuple-of-pairs literal."""
    if isinstance(node, ast.Dict):
        pairs = list(zip(node.keys, node.values, strict=True))
    else:
        pairs = [
            (element.elts[0], element.elts[1])
            for element in node.elts
            if isinstance(element, ast.Tuple) and len(element.elts) == 2
        ]
    return [row for key, value in pairs if (row := _vendor_count(key, value)) is not None]


def model_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, literal)`` of every literal in ``tree`` that states model knowledge.

    A string is matched whole and as part of a ``+`` chain of string constants,
    so splitting an id (``'claude' + '-opus-5-5'``) does not hide it.
    """
    found: list[tuple[int, str]] = []
    for node in walk_nodes(tree):
        if isinstance(node, ast.Constant | ast.BinOp):
            text = _folded_str(node)
            if text is not None and _states_a_model(text):
                found.append((node.lineno, text))
        if isinstance(node, ast.Dict | ast.Tuple | ast.List):
            found.extend((node.lineno, row) for row in _vendor_table_rows(node))
    return found


def _product_trees(root: Path) -> list[Path]:
    trees = [root / "src", *sorted((root / "libs").glob("*/src"))]
    return [tree for tree in trees if tree.is_dir()]


def product_python_files(root: Path) -> list[Path]:
    """Every Python file of the product trees under ``root``: ``src/`` and ``libs/*/src``."""
    return [path for tree in _product_trees(root) for path in python_files(tree)]


#: Product-tree files that are not text resources: code scanned as Python,
#: type markers, and build metadata.
_NOT_A_RESOURCE = re.compile(r"(\.py$|\.pyc$|\.typed$|/__pycache__/|\.egg-info/)")


def product_resource_files(root: Path) -> list[Path]:
    """Every non-Python text file of the product trees: templates, bundled TOML, JSON, YAML."""
    return [
        path
        for tree in _product_trees(root)
        for path in sorted(tree.rglob("*"))
        if path.is_file() and not _NOT_A_RESOURCE.search(path.as_posix())
    ]


def _client_files(clients: Path) -> list[Path]:
    """Every file under ``clients``, not descending into installed or built output."""
    found: list[Path] = []
    for directory, subdirectories, files in os.walk(clients):
        subdirectories[:] = sorted(name for name in subdirectories if name not in _SKIPPED_DIRS)
        found.extend(Path(directory, name) for name in sorted(files))
    return found


def client_typescript_files(root: Path) -> list[Path]:
    """Every TypeScript and JavaScript product file under ``root/clients``, no tests."""
    clients = root / "clients"
    if not clients.is_dir():
        return []
    return [
        path
        for path in _client_files(clients)
        if _CLIENT_CODE.search(path.name)
        and not _TS_NON_PRODUCT.search("/" + path.relative_to(root).as_posix())
    ]


def text_model_literals(text: str) -> list[tuple[int, str]]:
    """``(line, match)`` of every model id in ``text``, read line by line."""
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
    for path in [*product_resource_files(root), *client_typescript_files(root)]:
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # a binary resource holds no source text
        found.extend((relative, line, literal) for line, literal in text_model_literals(text))
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
        "WINDOWS = {'anthropic': 200_000, 'openai': 400_000}",
        "WINDOWS = (('claude', 1_000_000), ('codex', 1_050_000))",
        "WINDOWS = [('Google', 1_048_576)]",
        "MODEL = 'claude' + '-opus-5-5'",
        "MODEL = 'gpt' + '-' + '6-luna'",
        "x = 'opus-5'",
        "x = 'sonnet-5'",
        "x = 'Opus 5.5'",
        "x = 'haiku-4-5[1m]'",
        "DEFAULTS = {'claude': 'opus-5-5'}",
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
        "x = '@anthropic-ai/claude-code'",
        "TIMEOUTS = {'claude': 300, 'codex': 600}",
        "LIMITS = {'claude': 4096}",
        "x = 'claude' + suffix",
        "x = 'magnum opus 5'",
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
        "clients/apps/desktop-shell/src/campaign-stream.ts",
        "clients/tools/scripts/j.mjs",
        "clients/apps/desktop-shell/dist/k.js",
        "src/vibesys/prompts/l.md.j2",
        "libs/vs-x/src/vs_x/m.toml",
        "libs/vs-x/src/vs_x.egg-info/PKG-INFO",
        "docs/n.md",
    )
    for relative in planted:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("M = 'claude-opus-5-5'\n", encoding="utf-8")

    assert sorted(line.split(":")[0] for line in violations(tmp_path)) == [
        "clients/apps/desktop-shell/src/campaign-stream.ts",
        "clients/features/y/src/ui/f.tsx",
        "clients/libs/x/src/e.ts",
        "clients/tools/scripts/j.mjs",
        "libs/vs-x/src/vs_x/b.py",
        "libs/vs-x/src/vs_x/m.toml",
        "src/a.py",
        "src/vibesys/prompts/l.md.j2",
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
