"""Hold the process-boundary rule: a value crossing a process boundary is a typed message.

See docs/contributing/process-boundaries.md. Two ratchets, each with an exact allowlist so a
removed use cannot make room for a new one:

``env-name`` (C2)
    Environment variable names written in product code. Every ``VIBESYS_*`` and ``XDG_*`` name
    is a contract between two processes, so each use is listed in
    ``scripts/boundaries_env_allowlist.json`` until a message carries it.

``env-read``
    A read of a ``VIBESYS_*`` variable by name (``os.Getenv``, ``os.environ`` and the like) anywhere
    but the environment codec (``cli/envcodec``). A variable VibeSys hands a process is a field of a
    message (``env_name`` in ``proto/vibesys/cli/v1/env.proto``) and is read by the codec, so a
    hand read is either out of scope for now or a bug. Each read left is listed in
    ``scripts/boundaries_env_reads_allowlist.json`` with its exact count and a reason, and the list
    only shrinks.

``unvalidated`` (C5)
    Messages of ``cli.v1`` that carry no protovalidate rule, and are not empty. A message with
    fields that nothing constrains is a message whose receiver trusts its peer. Each is listed in
    ``scripts/boundaries_unvalidated_allowlist.json`` until a rule goes on it; the list only
    shrinks. The other proto packages join when their contracts get rules.

``mechanism-word`` (C3)
    Message, field, enum and rpc names in ``.proto`` files that name a mechanism (a path, a pid,
    a socket, a sidecar, a provider) instead of what the peer needs. Each is listed in
    ``scripts/boundaries_proto_allowlist.json`` until it is renamed or its reason is written down.

Stdlib only, so CI's always-on job can run it before any environment is installed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

ENV_ALLOWLIST = "scripts/boundaries_env_allowlist.json"
ENV_READS_ALLOWLIST = "scripts/boundaries_env_reads_allowlist.json"
PROTO_ALLOWLIST = "scripts/boundaries_proto_allowlist.json"
UNVALIDATED_ALLOWLIST = "scripts/boundaries_unvalidated_allowlist.json"
DOC = "docs/contributing/process-boundaries.md"

PRODUCT_ROOTS = ("sdk/", "cli/", "orchestrator/", "gateway/", "clients/", "skills/")
"""Where the managed-run product lives. The Python path (src/, libs/, resources/) is deleted, not
migrated, so it is not held to the rule. Docs, examples, tests and the installer's own settings
(bootstrap, read by sh before any binary exists) are outside."""

PROTO_ROOTS = tuple(
    f"proto/vibesys/{package}/v1/" for package in ("orchestrator", "cli", "desktop", "evaluator")
)
"""The proto packages of the managed-run path. The other packages belong to the Python path."""

CODE_SUFFIXES = (".go", ".py", ".ts", ".tsx", ".js", ".mjs")
SKIPPED_PARTS = (
    "/gen/", "/_generated/", "/generated/", "/node_modules/", "/testdata/", "/tests/", "/e2e/",
    "/archcheck/",
)  # fmt: skip
SKIPPED_NAMES = (
    "_test.go", ".test.ts", ".test.tsx", ".spec.ts", ".d.ts", ".golden.json", "playwright.config.ts",
    "playwright.fixture.config.ts",
)  # fmt: skip

ENV_NAME = re.compile(
    r"""["'`]((?:VIBESYS|XDG)_[A-Z0-9_]*[A-Z0-9])(?=[=\s"'`])"""
    r"""|\b(?:process\.env|env)\.((?:VIBESYS|XDG)_[A-Z0-9_]*[A-Z0-9])\b"""
)

_VIBESYS_NAME = r"VIBESYS_[A-Z0-9_]*[A-Z0-9]"
ENV_READ = re.compile(
    r"""(?:\bos\.(?:Getenv|LookupEnv)|\b[gG]etenv|\b[lL]ookupEnv|\bos\.getenv|\bos\.environ\.get"""
    r"""|\benviron\.get)\(\s*["'`](""" + _VIBESYS_NAME + r""")["'`]"""
    r"""|\bos\.environ\[\s*["'](""" + _VIBESYS_NAME + r""")["']\s*\]"""
    r"""|\bprocess\.env(?:\.("""
    + _VIBESYS_NAME
    + r""")\b|\[\s*["']("""
    + _VIBESYS_NAME
    + r""")["']\s*\])"""
)
"""A read of a variable by its literal name, in Go, Python and TypeScript."""

CODEC_ROOTS = ("cli/envcodec/",)
"""The environment codec: the one place that reads a variable because a message field names it."""

MECHANISM_WORDS = frozenset(
    {
        "path", "dir", "directory", "file", "pid", "pgid", "process", "socket", "json", "sidecar",
        "provider", "worktree", "marketplace", "home", "xdg", "docker", "sqlite", "claude", "codex",
    }
)  # fmt: skip
"""Words that name how something is done. A name with one of them leaks an implementation."""


def is_product_code(path: str) -> bool:
    """Whether ``path`` is product source that may name an environment variable."""
    return (
        path.startswith(PRODUCT_ROOTS)
        and path.endswith(CODE_SUFFIXES)
        and not path.endswith(SKIPPED_NAMES)
        and not any(part in f"/{path}" for part in SKIPPED_PARTS)
        and not Path(path).name.startswith("test_")
        and Path(path).name != "conftest.py"
    )


def env_uses(sources: Mapping[str, str]) -> dict[str, dict[str, int]]:
    """Count each environment variable name written in each product file."""
    found: dict[str, dict[str, int]] = {}
    for path, text in sorted(sources.items()):
        if not is_product_code(path):
            continue
        names = Counter(left or right for left, right in ENV_NAME.findall(text))
        if names:
            found[path] = dict(sorted(names.items()))
    return found


def env_reads(sources: Mapping[str, str]) -> dict[str, dict[str, int]]:
    """Count each ``VIBESYS_*`` variable read by its literal name in each file but the codec's."""
    found: dict[str, dict[str, int]] = {}
    for path, text in sorted(sources.items()):
        if not is_product_code(path) or path.startswith(CODEC_ROOTS):
            continue
        names = Counter(next(name for name in groups if name) for groups in ENV_READ.findall(text))
        if names:
            found[path] = dict(sorted(names.items()))
    return found


def env_read_violations(
    sources: Mapping[str, str], allowed: Mapping[str, Mapping[str, Mapping[str, object]]]
) -> list[str]:
    """Diagnostics for every read the allowlist does not hold exactly, or holds without a reason.

    An entry is ``{"count": n, "reason": "..."}``. The count is exact so a removed read cannot make
    room for a new one, and the reason says why that read is not a message field yet.
    """
    found = env_reads(sources)
    problems: list[str] = []
    for path in sorted(set(found) | set(allowed)):
        have, want = found.get(path, {}), allowed.get(path, {})
        for name in sorted(set(have) | set(want)):
            count = have.get(name, 0)
            entry = want.get(name, {})
            listed = entry.get("count", 0)
            if count > listed:
                problems.append(
                    f"{path}: env-read: {name} read {count} times by name, allowlist {listed}; "
                    f"a variable is a field of a message (env_name in proto/vibesys/cli/v1/env.proto) "
                    f"and read through cli/envcodec ({DOC}); or list it in {ENV_READS_ALLOWLIST} "
                    "with a reason it is out of scope"
                )
            elif count < listed:
                problems.append(
                    f"{path}: env-read: {name} read {count} times by name, allowlist {listed}; "
                    f"a read was removed; shrink the entry in {ENV_READS_ALLOWLIST}"
                )
            elif count and not str(entry.get("reason", "")).strip():
                problems.append(
                    f"{path}: env-read: {name} is listed in {ENV_READS_ALLOWLIST} without a reason; "
                    "say why this read is not a message field yet"
                )
    return problems


def env_violations(
    sources: Mapping[str, str], allowed: Mapping[str, Mapping[str, int]]
) -> list[str]:
    """Diagnostics for every file and name whose count differs from the allowlist."""
    found = env_uses(sources)
    problems: list[str] = []
    for path in sorted(set(found) | set(allowed)):
        have, want = found.get(path, {}), allowed.get(path, {})
        for name in sorted(set(have) | set(want)):
            if have.get(name, 0) == want.get(name, 0):
                continue
            if have.get(name, 0) > want.get(name, 0):
                fix = (
                    "an environment variable is a contract between two processes; pass the value "
                    f"in a typed message instead ({DOC}), or list it in {ENV_ALLOWLIST} with the "
                    "message that will replace it"
                )
            else:
                fix = f"a use was removed; shrink the entry in {ENV_ALLOWLIST}"
            problems.append(
                f"{path}: env-name: {name} written {have.get(name, 0)} times, allowlist "
                f"{want.get(name, 0)}; {fix}"
            )
    return problems


_DECLARATION = re.compile(r"^\s*(message|enum|service|oneof)\s+(\w+)\s*\{")
_RPC = re.compile(r"^\s*rpc\s+(\w+)\s*\(")
_FIELD = re.compile(r"^\s*(?:repeated\s+|optional\s+)?(?:map\s*<[^>]*>|[\w.]+)\s+(\w+)\s*=\s*\d+")
_ENUM_VALUE = re.compile(r"^\s*(\w+)\s*=\s*-?\d+")


def words(name: str) -> list[str]:
    """Split a snake_case, SCREAMING_CASE or CamelCase name into lowercase words."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name))
    return [word.lower() for word in re.split(r"[\s_]+", spaced) if word]


def proto_names(text: str) -> list[str]:
    """Qualified names (``Message.field``) declared in one ``.proto`` file."""
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    names: list[str] = []
    stack: list[tuple[str, str]] = []
    for raw in stripped.splitlines():
        line = raw.split("//", 1)[0]
        qualified = ".".join(name for kind, name in stack if kind not in {"oneof", "block"})
        declaration, rpc = _DECLARATION.match(line), _RPC.match(line)
        opens = line.count("{")
        if declaration:
            kind, name = declaration.groups()
            names.append(f"{qualified}.{name}" if qualified else name)
            stack.append((kind, name))
            opens -= 1
        elif rpc:
            names.append(f"{qualified}.{rpc.group(1)}")
        elif stack and stack[-1][0] == "enum" and (value := _ENUM_VALUE.match(line)):
            names.append(f"{qualified}.{value.group(1)}")
        elif (
            qualified
            and (field := _FIELD.match(line))
            and field.group(1) not in {"option", "reserved"}
        ):
            names.append(f"{qualified}.{field.group(1)}")
        stack.extend([("block", "")] * opens)
        for _ in range(line.count("}")):
            if stack:
                stack.pop()
    return names


VALIDATED_ROOTS = ("proto/vibesys/cli/v1/",)
"""The packages whose messages must carry a rule. ``cli.v1`` has released builds, so its rules are
additive; the other managed-run packages are still being shaped and join once they have rules."""

_MESSAGE_OPEN = re.compile(r"^\s*message\s+(\w+)\s*\{")


def _unvalidated_in(text: str) -> list[str]:
    """The names of the messages in one file's text that have fields and no rule."""
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Each entry: [qualified name, depth outside it, has a field, has a rule]
    stack: list[list[object]] = []
    depth = 0
    names: list[str] = []
    for raw in stripped.splitlines():
        line = raw.split("//", 1)[0]
        opened = _MESSAGE_OPEN.match(line)
        if opened:
            outer = f"{stack[-1][0]}." if stack else ""
            stack.append([outer + opened.group(1), depth, False, False])
        # A one-line message holds its fields after its brace.
        body = line.split("{", 1)[1] if opened else line
        if stack:
            stack[-1][2] = stack[-1][2] or any(_FIELD.match(part) for part in body.split(";"))
            stack[-1][3] = stack[-1][3] or "buf.validate" in body
        depth += line.count("{") - line.count("}")
        while stack and depth <= int(str(stack[-1][1])):
            name, _, has_field, has_rule = stack.pop()
            if has_field and not has_rule:
                names.append(str(name))
    return sorted(names)


def unvalidated_hits(protos: Mapping[str, str]) -> dict[str, list[str]]:
    """The messages with fields and no ``buf.validate`` option of their own, by file.

    A nested message is judged on its own text, not its parent's. A message with no fields has
    nothing to constrain (an empty request, an empty settings marker), so it is exempt.
    """
    return {
        path: names
        for path, text in sorted(protos.items())
        if path.startswith(VALIDATED_ROOTS)
        and path.endswith(".proto")
        and (names := _unvalidated_in(text))
    }


def unvalidated_violations(
    protos: Mapping[str, str], allowed: Mapping[str, Iterable[str]]
) -> list[str]:
    """Diagnostics for each unlisted unvalidated message, and each listed one that has a rule now."""
    found = unvalidated_hits(protos)
    problems: list[str] = []
    for path in sorted(set(found) | set(allowed)):
        have, want = set(found.get(path, [])), set(allowed.get(path, []))
        problems.extend(
            f"{path}: unvalidated: {name} has fields and no protovalidate rule; add the rules its "
            f"receiver already assumes in the same file ({DOC}), or list it in "
            f"{UNVALIDATED_ALLOWLIST} only when it is new and has no rule to state"
            for name in sorted(have - want)
        )
        problems.extend(
            f"{path}: unvalidated: {name} is listed but is validated, empty or gone; "
            f"remove it from {UNVALIDATED_ALLOWLIST}"
            for name in sorted(want - have)
        )
    return problems


def mechanism_hits(protos: Mapping[str, str]) -> dict[str, list[str]]:
    """The declared names that contain a mechanism word, by file."""
    found: dict[str, list[str]] = {}
    for path, text in sorted(protos.items()):
        if not path.startswith(PROTO_ROOTS) or not path.endswith(".proto"):
            continue
        hits = sorted(
            {
                name
                for name in proto_names(text)
                if MECHANISM_WORDS.intersection(words(name.rsplit(".", 1)[-1]))
            }
        )
        if hits:
            found[path] = hits
    return found


def mechanism_violations(
    protos: Mapping[str, str], allowed: Mapping[str, Iterable[str]]
) -> list[str]:
    """Diagnostics for every mechanism name that is not listed, and every listed name that is gone."""
    found = mechanism_hits(protos)
    problems: list[str] = []
    for path in sorted(set(found) | set(allowed)):
        have, want = set(found.get(path, [])), set(allowed.get(path, []))
        problems.extend(
            f"{path}: mechanism-word: {name} names how something is done, not what the peer "
            f"needs; rename it to the fact it carries ({DOC}), or list it in "
            f"{PROTO_ALLOWLIST} when the peer really needs that fact"
            for name in sorted(have - want)
        )
        problems.extend(
            f"{path}: mechanism-word: {name} is listed but no longer declared; "
            f"remove it from {PROTO_ALLOWLIST}"
            for name in sorted(want - have)
        )
    return problems


def _tracked(root: Path) -> list[str]:
    """The tracked files, so untracked build output never counts."""
    argv = ["git", "ls-files", "-z"]
    # lint-waiver: LW-235003 [S603]; a fixed git argv with no shell; stdlib so CI runs it before installing the environment.
    listed = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=True)  # noqa: S603
    return [path for path in listed.stdout.split("\0") if path and (root / path).is_file()]


def _read(root: Path, paths: Iterable[str]) -> dict[str, str]:
    return {path: (root / path).read_text(encoding="utf-8", errors="replace") for path in paths}


BREAKING_SCRIPT = "scripts/check_proto_breaking.sh"
ORCHESTRATOR_PATH = "--path proto/vibesys/orchestrator/v1"


def release_violations(breaking_script: str) -> list[str]:
    """Diagnostics when the breaking check leaves the orchestrator contract out.

    A ``vibesys-v*`` tag makes the contract public; the release workflow runs this, so a tag cannot
    publish a contract that ``buf breaking`` does not hold. This reads the script's text only;
    ``scripts/tests/test_proto_breaking.sh`` runs buf to prove the path in it fails a breaking change.
    """
    active = [line for line in breaking_script.splitlines() if not line.lstrip().startswith("#")]
    # The directory itself; a path to one file inside it does not hold the rest of the contract.
    if any(re.search(re.escape(ORCHESTRATOR_PATH) + r"(\s|$)", line) for line in active):
        return []
    return [
        f"{BREAKING_SCRIPT}: release: the breaking check leaves out proto/vibesys/orchestrator/v1, "
        "which a tagged release makes a public contract; add "
        f"'{ORCHESTRATOR_PATH}' back to its buf breaking command ({DOC}), then tag again"
    ]


def _sources(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    paths = _tracked(root)
    return (
        _read(root, (path for path in paths if is_product_code(path))),
        _read(root, (path for path in paths if path.endswith(".proto"))),
    )


def violations(root: Path) -> list[str]:
    """Every violation of the ratchets in the tree under ``root``."""
    env_allowed = json.loads((root / ENV_ALLOWLIST).read_text(encoding="utf-8"))
    reads_allowed = json.loads((root / ENV_READS_ALLOWLIST).read_text(encoding="utf-8"))
    proto_allowed = json.loads((root / PROTO_ALLOWLIST).read_text(encoding="utf-8"))
    unvalidated_allowed = json.loads((root / UNVALIDATED_ALLOWLIST).read_text(encoding="utf-8"))
    sources, protos = _sources(root)
    return (
        env_violations(sources, env_allowed)
        + env_read_violations(sources, reads_allowed)
        + mechanism_violations(protos, proto_allowed)
        + unvalidated_violations(protos, unvalidated_allowed)
    )


def _write_reads(root: Path, sources: Mapping[str, str]) -> None:
    """Rewrite the read allowlist to today's counts, keeping the reasons already written.

    A read with no reason yet is written with an empty one, which the check refuses until a person
    says why the read is out of scope.
    """
    path = root / ENV_READS_ALLOWLIST
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    new = {
        file: {
            name: {"count": count, "reason": old.get(file, {}).get(name, {}).get("reason", "")}
            for name, count in names.items()
        }
        for file, names in env_reads(sources).items()
    }
    path.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    """Print each violation and fail; ``--write`` rewrites the allowlists, ``--release`` guards a tag."""
    root = Path(__file__).resolve().parent.parent
    if argv == ["--write"]:
        sources, protos = _sources(root)
        (root / ENV_ALLOWLIST).write_text(
            json.dumps(env_uses(sources), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _write_reads(root, sources)
        (root / PROTO_ALLOWLIST).write_text(
            json.dumps(mechanism_hits(protos), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (root / UNVALIDATED_ALLOWLIST).write_text(
            json.dumps(unvalidated_hits(protos), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 0
    if argv == ["--release"]:
        found = release_violations((root / BREAKING_SCRIPT).read_text(encoding="utf-8"))
    else:
        found = violations(root)
    for problem in found:
        sys.stdout.write(problem + "\n")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
