#!/usr/bin/env python3
"""Check vibesys-evaluator's public Python API (``sdk/vibesys-evaluator/AGENTS.md``).

1. ``api/public-api.txt`` is what griffe reads from the package: every name in
   each public module's ``__all__`` with its signature, and each class's public
   members. ``--write`` regenerates it; otherwise a difference fails with the diff.
2. ``griffe check`` against the last release (the newest
   ``sdk/vibesys-evaluator/go/vX.Y.Z`` tag) reports breaking changes;
   skipped, with a note, until a release exists. Breaks the user approved
   are recorded in ``api/approved-breaks.toml`` against that release tag; they
   are printed but do not fail, until the next release makes the file stale.
3. ``pyright --verifytypes vibesys_evaluator`` finds no public symbol without a
   complete type.
4. Each example under ``examples/`` imports and type-checks (pyright, basic)
   against the package.
5. The Go SDK's snapshots ``api/go-workload.txt`` and ``api/go-check.txt``,
   ``api/go-microservices.txt`` and ``api/go-k8s.txt``
   (``go doc -all`` of ``go/workload``, ``go/check``, ``go/microservices`` and ``go/k8s``; each package's Go
   test ``TestAPISnapshotIsCurrent`` keeps its snapshot equal to the code)
   lose no line of the last release's: a removed or changed declaration is a
   break. Skipped, with a note, for a snapshot no release has yet.

Usage:
    uv run python scripts/check_vibesys_evaluator_api.py [--write]
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import griffe

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO = Path(__file__).resolve().parent.parent
SDK = REPO / "sdk" / "vibesys-evaluator"
SRC = SDK / "python" / "src"
SNAPSHOT = SDK / "api" / "public-api.txt"
GO_SNAPSHOTS = (
    SDK / "api" / "go-workload.txt",
    SDK / "api" / "go-check.txt",
    SDK / "api" / "go-microservices.txt",
    SDK / "api" / "go-k8s.txt",
)
EXAMPLES = SDK / "examples"
PACKAGE = "vibesys_evaluator"
#: The public modules, in the snapshot's order.
PUBLIC_MODULES = (
    "vibesys_evaluator",
    "vibesys_evaluator.microservices",
    "vibesys_evaluator.microservices.k8s",
    "vibesys_evaluator.llm_serving",
    "vibesys_evaluator.llm_serving.agentx",
    "vibesys_evaluator.llm_serving.golden",
)
RELEASE_TAG_PREFIX = "sdk/vibesys-evaluator/go/v"
#: Releases before the module moved from ``loadgen`` to ``go`` carry this prefix; they still count.
LEGACY_RELEASE_TAG_PREFIX = "sdk/vibesys-evaluator/loadgen/v"
#: The user's approval of the breaking changes since one release (AGENTS.md, "Public API rule").
#: It names that release, so it stops applying once the next release is tagged.
APPROVED_BREAKS = SDK / "api" / "approved-breaks.toml"
#: Special methods that are not API of their own (construction, printing, comparison).
_NOT_API = frozenset({"__init__", "__post_init__", "__repr__", "__str__", "__eq__", "__hash__"})


def _parameters(function: griffe.Function) -> str:
    parts: list[str] = []
    star = False
    for parameter in function.parameters:
        if parameter.name in {"self", "cls"}:
            continue
        kind = parameter.kind
        if kind is griffe.ParameterKind.keyword_only and not star:
            parts.append("*")
            star = True
        prefix = ""
        if kind is griffe.ParameterKind.var_positional:
            prefix, star = "*", True
        elif kind is griffe.ParameterKind.var_keyword:
            prefix = "**"
        text = prefix + parameter.name
        if parameter.annotation is not None:
            text += f": {parameter.annotation}"
        if parameter.default is not None and not prefix:
            text += f" = {parameter.default}"
        parts.append(text)
    return f"({', '.join(parts)})"


def _signature(function: griffe.Function) -> str:
    returns = f" -> {function.returns}" if function.returns is not None else ""
    return _parameters(function) + returns


def _members(cls: griffe.Class, path: str) -> Iterator[str]:
    for name in sorted(cls.members):
        member = cls.members[name]
        if member.is_alias or member.inherited:
            continue
        if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
            continue
        if name in _NOT_API or (name.startswith("__") and not member.is_function):
            continue
        if isinstance(member, griffe.Function):
            yield f"{path}.{name}{_signature(member)}"
        elif isinstance(member, griffe.Attribute):
            annotation = member.annotation
            yield f"{path}.{name}: {annotation}" if annotation is not None else f"{path}.{name}"


def _class_line(cls: griffe.Class, path: str) -> str:
    bases = f"({', '.join(str(b) for b in cls.bases)})" if cls.bases else ""
    init = cls.members.get("__init__")
    signature = _signature(init) if isinstance(init, griffe.Function) else "()"
    return f"class {path}{bases}  {signature}"


def listing() -> str:
    """The snapshot text for the package as it is in the source tree."""
    package = griffe.load(
        PACKAGE,
        search_paths=[str(SRC)],
        resolve_aliases=True,
        extensions=griffe.load_extensions("dataclasses"),
    )
    lines: list[str] = []
    for module_path in PUBLIC_MODULES:
        module = (
            package if module_path == PACKAGE else package[module_path.removeprefix(PACKAGE + ".")]
        )
        exported = module.exports
        if exported is None:
            msg = f"{module_path} has no __all__"
            raise SystemExit(msg)
        for name in sorted(str(n) for n in exported):
            target = module[name].final_target if module[name].is_alias else module[name]
            path = f"{module_path}.{name}"
            if isinstance(target, griffe.Class):
                lines.append(_class_line(target, path))
                lines.extend(_members(target, path))
            elif isinstance(target, griffe.Function):
                lines.append(path + _signature(target))
            else:
                lines.append(f"{path}: {getattr(target, 'annotation', '')}")
    return "\n".join(lines) + "\n"


def check_snapshot(*, write: bool) -> bool:
    """Compare (or rewrite) the snapshot; True when it matches."""
    text = listing()
    if write:
        SNAPSHOT.write_text(text)
        print(f"wrote {SNAPSHOT.relative_to(REPO)}")
        return True
    current = SNAPSHOT.read_text()
    if current == text:
        print("public-api.txt matches the package")
        return True
    diff = difflib.unified_diff(
        current.splitlines(),
        text.splitlines(),
        "api/public-api.txt",
        "the package (griffe)",
        lineterm="",
    )
    print("\n".join(diff))
    print(
        "\nThe public API differs from api/public-api.txt. A breaking change needs the user's "
        "approval first (sdk/vibesys-evaluator/AGENTS.md); then regenerate the snapshot with "
        "--write and commit it with the change."
    )
    return False


def _run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401  # LW-150403 [ANN401]; subprocess.run's own keyword options, passed through.
    # > Naming each option would copy subprocess.run's signature for the four calls here.
    """Run one of this check's tools (git, uv, pyright, the interpreter) without a shell."""
    # lint-waiver: LW-150402 [S603]; every argv here is a fixed tool with this check's own arguments.
    # > A shell would add quoting risk; resolving each tool's absolute path would tie the check to one
    # > host layout. One helper keeps the single opt-out in one place.
    return subprocess.run(argv, text=True, check=False, **options)  # noqa: S603


def _last_release() -> str | None:
    git = shutil.which("git") or "git"
    newest: list[tuple[str, str]] = []
    for prefix in (RELEASE_TAG_PREFIX, LEGACY_RELEASE_TAG_PREFIX):
        tags = _run(
            [git, "tag", "--list", f"{prefix}*", "--sort=-v:refname"],
            cwd=REPO,
            capture_output=True,
        ).stdout.split()
        if tags:
            newest.append((tags[0].removeprefix(prefix), tags[0]))
    if not newest:
        return None
    # A tag of the new prefix wins a tie: the same version was re-cut under the module's new path.
    versions = sorted(
        newest, key=lambda pair: (_semver_key(pair[0]), pair[1].startswith(RELEASE_TAG_PREFIX))
    )
    return versions[-1][1]


def _semver_key(version: str) -> tuple[tuple[int, ...], int, str]:
    """Order ``0.2.0-rc.1`` before ``0.2.0`` (a release candidate precedes its release)."""
    core, _, pre = version.partition("-")
    numbers = tuple(int(part) for part in core.split(".") if part.isdigit())
    return numbers, 0 if pre else 1, pre


def check_release() -> bool:
    """``griffe check`` against the last release; True when nothing breaks (or no release yet)."""
    tag = _last_release()
    if tag is None:
        print(f"griffe check: no release tag {RELEASE_TAG_PREFIX}* yet; skipped")
        return True
    done = _run(
        [
            sys.executable,
            "-m",
            "griffe",
            "check",
            PACKAGE,
            "-s",
            str(SRC.relative_to(REPO)),
            "-a",
            tag,
            "--verbose",
        ],
        cwd=REPO,
    )
    if done.returncode == 0:
        return True
    approved = _approved_break(tag)
    if approved is None:
        return False
    print(
        f"griffe check: the breaks above against {tag} are approved in "
        f"{APPROVED_BREAKS.relative_to(REPO)}: {approved}"
    )
    return True


def _approved_break(tag: str) -> str | None:
    """The approval of breaking changes against ``tag``, if the user gave one (its reason)."""
    if not APPROVED_BREAKS.is_file():
        return None
    approval = tomllib.loads(APPROVED_BREAKS.read_text())
    if set(approval) != {"against", "approved_by", "reason"}:
        msg = f"{APPROVED_BREAKS}: keys are against, approved_by and reason"
        raise SystemExit(msg)
    if approval["against"] != tag:
        print(
            f"griffe check: {APPROVED_BREAKS.relative_to(REPO)} approves breaks against "
            f"{approval['against']}, not the last release {tag}; delete it"
        )
        return None
    return f"{approval['reason']} (approved by {approval['approved_by']})"


def check_go_release() -> bool:
    """Each Go snapshot keeps every line of the last release's; True when nothing is removed."""
    results = [_check_go_snapshot(snapshot) for snapshot in GO_SNAPSHOTS]
    return all(results)


def _check_go_snapshot(snapshot: Path) -> bool:
    tag = _last_release()
    path = snapshot.relative_to(REPO).as_posix()
    git = shutil.which("git") or "git"
    released = _run([git, "show", f"{tag}:{path}"], cwd=REPO, capture_output=True) if tag else None
    if released is None or released.returncode != 0:
        print(f"go api: no release has {path} yet; skipped")
        return True
    current = set(snapshot.read_text().splitlines())
    removed = [
        line for line in released.stdout.splitlines() if line.strip() and line not in current
    ]
    if not removed:
        print(f"go api: every declaration of {tag}'s {path} is kept")
        return True
    print(f"go api: lines of {tag}'s {path} that changed or went away:")
    print("\n".join(f"  {line}" for line in removed))
    approved = _approved_break(tag)
    if approved is not None:
        print(
            f"go api: the changes above are approved in {APPROVED_BREAKS.relative_to(REPO)}: {approved}"
        )
        return True
    print(
        "A breaking change of the Go SDK needs the user's approval first "
        "(sdk/vibesys-evaluator/AGENTS.md)."
    )
    return False


def _pyright(
    *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    pyright = shutil.which("pyright") or str(Path(sys.executable).parent / "pyright")
    environment = {**os.environ, "PYRIGHT_PYTHON_IGNORE_WARNINGS": "1", **(env or {})}
    return _run([pyright, *args], cwd=cwd, capture_output=True, env=environment)


def check_verifytypes() -> bool:
    """``pyright --verifytypes``: every public symbol fully typed.

    verifytypes reads an installed package and cannot follow the workspace's
    editable install, so the package is installed into a throwaway virtual
    environment first.
    """
    uv = shutil.which("uv")
    if uv is None:
        print("verifytypes: uv is not on PATH")
        return False
    with tempfile.TemporaryDirectory() as scratch:
        venv = Path(scratch) / "venv"
        # A copy is built, not the tree: setuptools leaves build/ and *.egg-info beside the source.
        source = Path(scratch) / "python"
        shutil.copytree(
            SDK / "python",
            source,
            ignore=shutil.ignore_patterns("tests", "build", "*.egg-info", "__pycache__"),
        )
        for argv in (
            [uv, "venv", "-q", "-p", sys.executable, str(venv)],
            [
                uv,
                "pip",
                "install",
                "-q",
                "--python",
                str(venv / "bin" / "python"),
                str(source),
            ],
        ):
            if _run(argv).returncode != 0:
                print(f"verifytypes: {' '.join(argv)} failed")
                return False
        done = _pyright(
            "--verifytypes",
            PACKAGE,
            "--ignoreexternal",
            "--outputjson",
            cwd=Path(scratch),
            env={"VIRTUAL_ENV": str(venv), "PATH": f"{venv / 'bin'}:{os.environ.get('PATH', '')}"},
        )
    try:
        report = json.loads(done.stdout)
    except json.JSONDecodeError:
        print(done.stdout + done.stderr)
        return False
    completeness = report["typeCompleteness"]
    problems = [d for d in report["generalDiagnostics"] if d["severity"] == "error"]
    for problem in problems:
        print(f"verifytypes: {problem.get('file', '')}: {problem['message']}")
    score = completeness["completenessScore"]
    print(f"verifytypes: {completeness['exportedSymbolCounts']} completeness {score:.0%}")
    return score == 1.0 and not problems


def check_examples() -> bool:
    """Every example imports, and type-checks with pyright against the package."""
    ok = True
    examples = sorted(p for p in EXAMPLES.iterdir() if (p / "bench.py").is_file())
    for example in examples:
        done = _run([sys.executable, "-c", "import bench"], cwd=example, capture_output=True)
        if done.returncode != 0:
            print(f"{example.name}: import bench failed:\n{done.stderr[-2000:]}")
            ok = False
    # The examples' own configuration: one execution environment per example, so each one's
    # `import dataset` is its own. Imports of the task image's client libraries (pymongo,
    # redis) are warnings: they are not the package's.
    done = _pyright("-p", str(EXAMPLES / "pyrightconfig.json"), cwd=REPO)
    summary = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stderr
    print(f"examples: pyright {summary}")
    if done.returncode != 0:
        print(done.stdout)
        ok = False
    return ok


def main() -> int:
    """Run every check; exit 1 when any fails."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--write", action="store_true", help="regenerate api/public-api.txt")
    args = parser.parse_args()
    results = [
        check_snapshot(write=args.write),
        check_release(),
        check_go_release(),
        check_verifytypes(),
        check_examples(),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
