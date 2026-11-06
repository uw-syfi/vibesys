"""A Go build must not register the same protobuf package twice."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# A module that is published on its own cannot require a local module (a `replace` does not reach
# its dependents) and has no released module to require instead, so it keeps a private copy in an
# internal package. Evaluators use that module alone, never beside the module the copy comes from.
STANDALONE_COPIES = {
    ("github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval", "vibesys/evaluator/v1"),
}


def proto_copy_violations(files: dict[str, str]) -> list[str]:
    """Check module-local generation and transitive local module requirements."""
    modules = {
        str(Path(name).parent): _module_name(name, text)
        for name, text in files.items()
        if name.endswith("go.mod")
    }
    copies = _copies(files, modules)
    owners: dict[str, set[str]] = defaultdict(set)
    for module, packages in copies.items():
        for package in packages:
            if (module, package) not in STANDALONE_COPIES:
                owners[package].add(module)
    return [
        f"{package} generated in {sorted(found)}"
        for package, found in owners.items()
        if len(found) > 1
    ] + [
        f"{module}: {package} generated twice: {sorted(trees)}"
        for module, packages in copies.items()
        for package, trees in packages.items()
        if len(trees) > 1
    ]


def _module_name(name: str, text: str) -> str:
    match = re.search(r"(?m)^module\s+(\S+)", text)
    assert match is not None, f"{name}: missing module declaration"
    return match[1]


def _copies(files: dict[str, str], modules: dict[str, str]) -> dict[str, dict[str, set[str]]]:
    copies: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for name, text in files.items():
        if not name.endswith(".pb.go"):
            continue
        owner = max((d for d in modules if name.startswith(d + "/") or d == "."), key=len)
        source = re.search(r"(?m)^// source: (.+\.proto)$", text)
        if source:
            # Repository contracts generate one Go package per proto directory.
            # Well-known third-party types intentionally use one Go package per file.
            package = str(Path(source[1]).parent) if source[1].startswith("vibesys/") else source[1]
            copies[modules[owner]][package].add(str(Path(name).parent))
    return copies


def test_go_modules_generate_and_link_each_proto_package_once() -> None:
    files = {
        str(p.relative_to(ROOT)): p.read_text(encoding="utf-8")
        for pattern in ("**/go.mod", "**/go.work", "**/*.pb.go")
        for p in ROOT.glob(pattern)
        if not any(part in {"node_modules", ".venv", ".git"} for part in p.parts)
    }
    assert not proto_copy_violations(files)


def test_proto_copy_check_rejects_duplicates_across_modules() -> None:
    files = {
        "cli/go.mod": "module example/cli",
        "orchestrator/go.mod": "module example/orchestrator",
        "cli/gen/service.pb.go": "// source: vibesys/cli/v1/service.proto\n",
        "orchestrator/gen/service.pb.go": "// source: vibesys/cli/v1/service.proto\n",
    }
    assert proto_copy_violations(files)
