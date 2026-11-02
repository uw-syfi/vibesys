#!/usr/bin/env python3
"""Check commit messages against the vibesys commit schema and install the commit-msg hook.

Standard library only, Python 3.10 or newer; it runs from an installed skill directory and
imports no VibeSys package. The schema is Conventional Commits with Git's usual message rules:

    <type>(<optional scope>): <subject>

    <body, optional, wrapped at 72>

    Checks: pass|fail|not-run
    Plan: NNN          (required on a vibesys-work/<task>/plan-NNN branch; must match NNN)

Types: feat fix perf refactor test docs build chore revert, and ``wip``. A ``wip`` commit is a
checkpoint: only its first line (``wip: <note>`` or ``wip(<scope>): <note>``) is checked, it needs
no trailers, and it can never be measured or handed in.

Subcommands:

* ``check-message FILE [--branch B] [--require-pass]``: check one message file (the commit-msg
  hook calls this; Git's ``#`` comment lines are ignored).
* ``check-range BASE..HEAD [--branch B] [--repo DIR]``: check every commit in the range. ``wip`` commits only
  need a valid first line; the head must be a full commit with ``Checks: pass``.
* ``install-hook --dir STATE [--worktree PATH ...]``: add the commit-msg hook to the
  optimization's worktrees, chaining to any hook the repository already has.

Exit codes: 0 valid, 1 violations (each names the commit, the rule and the fix), 2 invalid input.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

__all__ = [
    "CHECKS_VALUES",
    "MAX_LINE",
    "TYPES",
    "InvalidInputError",
    "Message",
    "Trailer",
    "Violation",
    "check_commits",
    "check_message",
    "check_message_bytes",
    "main",
    "parse_message",
    "strip_comments",
]

TYPES = ("feat", "fix", "perf", "refactor", "test", "docs", "build", "chore", "revert")
"""The Conventional Commits types a full commit may use."""
WIP = "wip"
CHECKS_VALUES = ("pass", "fail", "not-run")
MAX_LINE = 72

_HEADER = re.compile(
    r"(?P<type>[a-z]+)(?:\((?P<scope>[A-Za-z0-9._/-]+)\))?(?P<bang>!)?:(?: (?P<subject>.*))?"
)
_TRAILER = re.compile(r"(?P<key>[A-Za-z0-9-]+)[ \t]*:[ \t]*(?P<value>.*)")
_RECOGNIZED = ("Signed-off-by:", "(cherry picked from commit ")  # the prefixes Git itself knows
_PLAN_BRANCH = re.compile(r"vibesys-work/[^/]+/plan-(?P<number>[0-9]+)")
_URL = re.compile(r"https?://\S+")
_SCISSORS = re.compile(r"# -+ >8 -+")
_WORD = re.compile(r"[A-Za-z]+")

# The imperative check is a heuristic on the first word: it flags past tense (-ed), gerunds
# (-ing) and third person (-s). These common imperatives share those endings and are allowed.
_NOT_PAST = frozenset({"speed", "embed", "proceed", "succeed", "exceed", "shred", "bleed"})
_NOT_GERUND = frozenset({"string", "spring", "during", "nothing", "anything", "something"})
_NOT_THIRD = frozenset({"alias", "canvas", "bias", "atlas", "chaos", "bus", "gas"})
_ONE_PARENT_FIELDS = 2  # `rev-list --parents` prints the commit and its parents
_SHORTEST_FLAGGED = {"ed": 5, "ing": 6, "s": 4}


class InvalidInputError(Exception):
    """The arguments or the files are not usable; the message says which part."""


@dataclass(frozen=True)
class Violation:
    """One broken rule of one commit, with the way to fix it."""

    commit: str
    rule: str
    problem: str
    fix: str

    def __str__(self) -> str:
        """The line printed for this violation."""
        return f"{self.commit}: {self.rule}: {self.problem} Fix: {self.fix}"


@dataclass(frozen=True)
class Trailer:
    """One ``Key: value`` line of the trailer block."""

    key: str
    value: str


@dataclass(frozen=True)
class Message:
    """A commit message split into its parts."""

    lines: tuple[str, ...]
    trailers: tuple[Trailer, ...]
    trailer_start: int
    """Index of the first trailer line, or ``len(lines)`` when there is no trailer block."""

    @property
    def header(self) -> str:
        """The first line, or an empty string for an empty message."""
        return self.lines[0] if self.lines else ""


# --- parsing -----------------------------------------------------------------------------------


def strip_comments(text: str) -> str:
    """Remove what Git removes before it commits: ``#`` lines, the scissors tail, outer blanks."""
    kept: list[str] = []
    for line in text.split("\n"):
        if _SCISSORS.fullmatch(line.rstrip()):
            break
        if not line.startswith("#"):
            kept.append(line)
    return "\n".join(kept).strip("\n")


def _trailer_block(lines: Sequence[str]) -> tuple[int, tuple[Trailer, ...]]:
    """Where the trailer block starts and its trailers, by ``git interpret-trailers`` rules.

    The last paragraph is a trailer block when every line is a ``Key: value`` line (or a
    continuation of one), or when it holds a line Git itself recognizes and at least a quarter of
    its lines are trailers. The first paragraph is the title and never a trailer block.
    """
    none = (len(lines), ())
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start and lines[start - 1].strip():
        start -= 1
    if start in {0, end}:
        return none
    found: list[Trailer] = []
    others = 0
    recognized = False
    continues = False  # whether the previous line was a trailer or a continuation of one
    for line in lines[start:end]:
        match = _TRAILER.fullmatch(line)
        recognized = recognized or line.startswith(_RECOGNIZED)
        if line[:1] in {" ", "\t"} and continues:
            found[-1] = Trailer(found[-1].key, f"{found[-1].value} {line.strip()}".strip())
        elif match and line[:1] not in {" ", "\t"}:
            found.append(Trailer(match["key"], match["value"].strip()))
            continues = True
        else:
            others += 1
            continues = False
    if found and (others == 0 or (recognized and len(found) * 3 >= others)):
        return start, tuple(found)
    return none


def parse_message(text: str) -> Message:
    """Split a message into lines and its trailer block. Never raises."""
    lines = text.rstrip().split("\n") if text.strip() else []
    start, trailers = _trailer_block(lines)
    return Message(tuple(lines), trailers, start)


# --- the schema --------------------------------------------------------------------------------


def _imperative_problem(subject: str) -> str | None:
    """The reason the first word is not imperative, or ``None`` when it looks fine."""
    match = _WORD.match(subject)
    if not match or match.end() != len(subject.split(maxsplit=1)[0]):
        return None
    word = match[0].lower()
    if word.endswith("ed") and len(word) >= _SHORTEST_FLAGGED["ed"] and word not in _NOT_PAST:
        kind = "past tense"
    elif word.endswith("ing") and len(word) >= _SHORTEST_FLAGGED["ing"] and word not in _NOT_GERUND:
        kind = "a gerund"
    elif (
        word.endswith("s")
        and not word.endswith(("ss", "us", "is", "os"))
        and len(word) >= _SHORTEST_FLAGGED["s"]
        and word not in _NOT_THIRD
    ):
        kind = "third person"
    else:
        return None
    return f"`{match[0]}` reads as {kind}"


def _wrapped_problems(lines: Sequence[str]) -> list[tuple[int, int]]:
    """Body lines over the limit as (line number, length), skipping URLs and code."""
    found = []
    fenced = False
    for number, line in enumerate(lines[1:], start=2):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        exempt = fenced or line.startswith(("    ", "\t")) or _URL.search(line)
        if len(line) > MAX_LINE and not exempt:
            found.append((number, len(line)))
    return found


def _header_violations(commit: str, header: str) -> tuple[list[Violation], str | None]:
    """Violations of the first line and, when it parses, the type."""
    match = _HEADER.fullmatch(header)
    if not match:
        return [
            Violation(
                commit,
                "header-format",
                f"the first line {header[:60]!r} is not `<type>(<scope>): <subject>`.",
                "write `<type>: <subject>` or `<type>(<scope>): <subject>` with a lowercase "
                f"type from {', '.join(TYPES)}, or `wip: <note>` for a checkpoint.",
            )
        ], None
    found: list[Violation] = []
    kind = match["type"]
    subject = (match["subject"] or "").strip()
    if kind != WIP and kind not in TYPES:
        found.append(
            Violation(
                commit,
                "type-unknown",
                f"type `{kind}` is not allowed.",
                f"use one of {', '.join(TYPES)}, or `wip` for a checkpoint.",
            )
        )
    if kind == WIP and match["bang"]:
        found.append(Violation(commit, "header-format", "`wip` takes no `!`.", "drop the `!`."))
    if not subject:
        found.append(
            Violation(
                commit,
                "wip-note-empty" if kind == WIP else "subject-empty",
                "there is nothing after the colon.",
                "write a short note after `: `.",
            )
        )
    elif kind != WIP:
        found.extend(_subject_violations(commit, header, subject))
    return found, kind


def _subject_violations(commit: str, header: str, subject: str) -> list[Violation]:
    found: list[Violation] = []
    if subject.endswith("."):
        found.append(
            Violation(commit, "subject-period", "the subject ends with a period.", "drop it.")
        )
    if len(header) > MAX_LINE:
        found.append(
            Violation(
                commit,
                "subject-length",
                f"the first line is {len(header)} characters; the limit is {MAX_LINE}.",
                "shorten the subject and move detail to the body.",
            )
        )
    problem = _imperative_problem(subject)
    if problem:
        found.append(
            Violation(
                commit,
                "subject-imperative",
                f"{problem}.",
                "start with an imperative verb: `add`, `fix`, `remove`, not `added`, "
                "`adding` or `adds`.",
            )
        )
    return found


def _trailer_violations(
    commit: str, message: Message, branch: str | None, *, require_pass: bool
) -> list[Violation]:
    found: list[Violation] = []
    where = "put trailers in their own last paragraph, after a blank line, one `Key: value` each."
    checks = [t.value for t in message.trailers if t.key == "Checks"]
    if not checks:
        found.append(
            Violation(
                commit,
                "checks-missing",
                "there is no `Checks:` trailer.",
                f"add `Checks: pass`, `Checks: fail` or `Checks: not-run`; {where}",
            )
        )
    elif len(checks) > 1:
        found.append(Violation(commit, "checks-duplicate", "`Checks:` appears twice.", "keep one."))
    elif checks[0] not in CHECKS_VALUES:
        found.append(
            Violation(
                commit,
                "checks-value",
                f"`Checks: {checks[0]}` is not a valid value.",
                f"use one of {', '.join(CHECKS_VALUES)}.",
            )
        )
    elif require_pass and checks[0] != "pass":
        found.append(
            Violation(
                commit,
                "checks-not-pass",
                f"`Checks: {checks[0]}`, and a candidate must say `Checks: pass`.",
                "run the checks, fix what fails, and commit again on top with `Checks: pass`.",
            )
        )
    plans = [t.value for t in message.trailers if t.key == "Plan"]
    wanted = _PLAN_BRANCH.fullmatch(branch or "")
    if len(plans) > 1 or any(not re.fullmatch(r"[0-9]+", p) for p in plans):
        found.append(
            Violation(commit, "plan-value", "`Plan:` must be one number.", "write `Plan: NNN`.")
        )
    elif wanted and not plans:
        found.append(
            Violation(
                commit,
                "plan-missing",
                f"branch `{branch}` needs a `Plan:` trailer.",
                f"add `Plan: {wanted['number']}`; {where}",
            )
        )
    elif wanted and int(plans[0]) != int(wanted["number"]):
        found.append(
            Violation(
                commit,
                "plan-mismatch",
                f"`Plan: {plans[0]}` does not match branch `{branch}`.",
                f"write `Plan: {wanted['number']}`.",
            )
        )
    return found


def check_message(
    text: str, *, commit: str, branch: str | None = None, require_pass: bool = False
) -> list[Violation]:
    """Every violation of ``text`` as a commit message; empty means valid. Never raises.

    ``branch`` enables the ``Plan`` rule on ``vibesys-work/<task>/plan-NNN`` branches.
    ``require_pass`` is the candidate rule: a full commit whose ``Checks`` is ``pass``.
    """
    if "\x00" in text:
        return [Violation(commit, "encoding", "the message holds a NUL byte.", "remove it.")]
    message = parse_message(text)
    if not message.lines:
        return [
            Violation(
                commit,
                "message-empty",
                "the message is empty.",
                "write `<type>: <subject>`, or `wip: <note>` for a checkpoint.",
            )
        ]
    found, kind = _header_violations(commit, message.header)
    if kind == WIP:
        if require_pass and not found:
            found.append(
                Violation(
                    commit,
                    "wip-not-measurable",
                    "a `wip` commit cannot be measured or handed in.",
                    "make a real schema-valid commit on top with `Checks: pass`; the `wip` "
                    "commits below it stay as they are.",
                )
            )
        return found
    if len(message.lines) > 1 and message.lines[1].strip():
        found.append(
            Violation(
                commit,
                "blank-after-subject",
                "the second line is not blank.",
                "leave one blank line between the subject and the rest.",
            )
        )
    found.extend(_trailer_violations(commit, message, branch, require_pass=require_pass))
    found.extend(
        Violation(
            commit,
            "body-line-length",
            f"line {number} is {length} characters; the limit is {MAX_LINE}.",
            "wrap it (URLs and indented or fenced code may stay long).",
        )
        for number, length in _wrapped_problems(message.lines)
    )
    return found


def check_message_bytes(
    raw: bytes, *, commit: str, branch: str | None = None, require_pass: bool = False
) -> list[Violation]:
    """``check_message`` for bytes: text that is not UTF-8 is a violation, not a crash."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [Violation(commit, "encoding", "the message is not valid UTF-8.", "re-encode it.")]
    return check_message(text, commit=commit, branch=branch, require_pass=require_pass)


def check_commits(
    commits: Sequence[tuple[str, bytes]], *, branch: str | None = None
) -> list[Violation]:
    """Check commits (oldest first, as ``(name, raw message)``); the last is the candidate head.

    Earlier commits need a valid message, and a ``wip`` one only a valid first line. The head
    must be a full commit with ``Checks: pass``.
    """
    found: list[Violation] = []
    for index, (name, raw) in enumerate(commits):
        found.extend(
            check_message_bytes(
                raw, commit=name, branch=branch, require_pass=index == len(commits) - 1
            )
        )
    return found


# --- git ---------------------------------------------------------------------------------------

_GIT = shutil.which("git") or "git"


def _git(cwd: Path, *argv: str, check: bool = True) -> bytes:
    done = subprocess.run(  # noqa: S603  # LW-100374; fixed git argv, no shell.
        [_GIT, "-C", str(cwd), *argv], check=False, capture_output=True
    )
    # > A shell adds quoting hazards for branch names; a wrapper hides one fixed command.
    if check and done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise InvalidInputError(f"git {' '.join(argv)} failed: {detail}")
    return done.stdout if done.returncode == 0 else b""


def _text(cwd: Path, *argv: str, check: bool = True) -> str:
    return _git(cwd, *argv, check=check).decode("utf-8", "replace").strip()


def _raw_message(cwd: Path, sha: str) -> bytes:
    """The commit's message bytes: what follows the first blank line of the commit object."""
    _, _, message = _git(cwd, "cat-file", "commit", sha).partition(b"\n\n")
    return message


def _check_range(cwd: Path, spec: str, branch: str | None) -> list[Violation]:
    base, dots, head = spec.partition("..")
    if not dots or not base or not head or head.startswith(".") or ".." in head:
        raise InvalidInputError(f"{spec!r} is not a range: write <base>..<head>")
    listed = _text(cwd, "rev-list", "--reverse", "--parents", spec).splitlines()
    if not listed:
        raise InvalidInputError(f"the range {spec} holds no commits")
    shas = [line.split() for line in listed]
    if branch is None:
        branch = (
            head
            if head != "HEAD"
            else _text(cwd, "symbolic-ref", "-q", "--short", "HEAD", check=False)
        )
    commits = [
        (parts[0][:12], _raw_message(cwd, parts[0]))
        for index, parts in enumerate(shas)
        if len(parts) == _ONE_PARENT_FIELDS
        or index == len(shas) - 1  # skip merges, but never the head
    ]
    return check_commits(commits, branch=branch or None)


# --- install-hook ------------------------------------------------------------------------------

STATE_PARENT = (".vibesys-state", "optimize")
_HOOK_HEADER = "#!/bin/sh\n# Generated by commit_schema.py install-hook; run it again to refresh.\n"


def _repo_root(state: Path) -> Path:
    if state.parent.parts[-2:] != STATE_PARENT:
        raise InvalidInputError(
            f"{state} is not a state directory: expected <repo>/{'/'.join(STATE_PARENT)}/<task>"
        )
    return state.parents[2]


def _commit_msg_hook(script: Path, previous: Path) -> str:
    q = shlex.quote
    return (
        f"{_HOOK_HEADER}"
        f'[ -f {q(str(script))} ] || {{ echo "commit_schema.py moved: run install-hook again" >&2; exit 1; }}\n'
        "branch=$(git symbolic-ref -q --short HEAD 2>/dev/null || true)\n"
        f'{q(sys.executable)} {q(str(script))} check-message "$1" ${{branch:+--branch "$branch"}} || exit $?\n'
        f'if [ -x {q(str(previous / "commit-msg"))} ]; then exec {q(str(previous / "commit-msg"))} "$@"; fi\n'
        "exit 0\n"
    )


def _forwarder(target: Path) -> str:
    return f'{_HOOK_HEADER}exec {shlex.quote(str(target))} "$@"\n'


def _hooks_in_use(worktree: Path) -> Path:
    """The directory Git runs this worktree's hooks from right now."""
    configured = _text(worktree, "config", "--get", "core.hooksPath", check=False)
    if configured:
        path = Path(configured)
        return (
            path
            if path.is_absolute()
            else Path(_text(worktree, "rev-parse", "--show-toplevel")) / path
        )
    common = Path(_text(worktree, "rev-parse", "--git-common-dir"))
    return (common if common.is_absolute() else worktree / common) / "hooks"


def _install_one(state: Path, worktree: Path, script: Path) -> str:
    if _text(worktree, "config", "--bool", "core.bare", check=False) == "true" or _text(
        worktree, "config", "--local", "core.worktree", check=False
    ):
        raise InvalidInputError(
            f"{worktree}: the repository sets core.bare or core.worktree, which per-worktree "
            "configuration would move; install the hook by hand"
        )
    top = Path(_text(worktree, "rev-parse", "--show-toplevel")).resolve()
    slug = f"{top.name}-{hashlib.sha256(str(top).encode()).hexdigest()[:8]}"
    ours = state / "hooks" / slug
    current = _hooks_in_use(top)
    remembered = ours / "previous"
    if current.resolve() == ours.resolve():
        if not remembered.is_file():
            raise InvalidInputError(f"{ours} lost its `previous` file: remove it and run again")
        previous = Path(remembered.read_text(encoding="utf-8").strip())
    else:
        previous = current
    ours.mkdir(parents=True, exist_ok=True)
    remembered.write_text(f"{previous}\n", encoding="utf-8")
    written = {"commit-msg": _commit_msg_hook(script, previous)}
    if previous.is_dir():
        for hook in sorted(previous.iterdir()):
            if hook.is_file() and hook.suffix != ".sample" and hook.name != "commit-msg":
                written[hook.name] = _forwarder(hook)
    for stale in ours.iterdir():
        if stale.name not in written and stale.name != "previous":
            stale.unlink()
    for name, body in written.items():
        (ours / name).write_text(body, encoding="utf-8")
        (ours / name).chmod(0o755)
    _git(top, "config", "--local", "extensions.worktreeConfig", "true")
    _git(top, "config", "--worktree", "core.hooksPath", str(ours))
    return f"{top}: commit-msg installed in {ours}, chained to {previous}"


def _state_worktrees(state: Path) -> list[Path]:
    root = _repo_root(state)
    listing = _text(root, "worktree", "list", "--porcelain")
    paths = [Path(line[9:]) for line in listing.splitlines() if line.startswith("worktree ")]
    inside = (state / "worktrees").resolve()
    return [p for p in paths if p.resolve().is_relative_to(inside)]


def _install_hook(state: Path, worktrees: list[Path], out: TextIO) -> int:
    if not state.is_dir():
        raise InvalidInputError(f"the state directory {state} does not exist")
    targets = worktrees or _state_worktrees(state)
    if not targets:
        raise InvalidInputError(f"no worktree under {state / 'worktrees'}: pass --worktree PATH")
    script = Path(__file__).resolve()
    out.writelines(_install_one(state, target, script) + "\n" for target in targets)
    return 0


# --- command line ------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise InvalidInputError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="commit_schema.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command_name", required=True, parser_class=_Parser)
    message = commands.add_parser("check-message", help="check one commit message file")
    message.add_argument("file", type=Path)
    message.add_argument("--branch", help="enables the Plan rule on plan branches")
    message.add_argument("--require-pass", action="store_true", help="the candidate rule")
    span = commands.add_parser("check-range", help="check every commit in BASE..HEAD")
    span.add_argument("range")
    span.add_argument("--branch", help="default: the HEAD side of the range")
    span.add_argument("--repo", type=Path, default=Path(), help="default: the current directory")
    hook = commands.add_parser("install-hook", help="add the commit-msg hook to worktrees")
    hook.add_argument("--dir", required=True, type=Path, help="the optimization state directory")
    hook.add_argument("--worktree", action="append", default=[], type=Path)
    return parser


def _report(violations: Sequence[Violation], out: TextIO, err: TextIO) -> int:
    err.writelines(f"{violation}\n" for violation in violations)
    if not violations:
        out.write("ok\n")
    return 1 if violations else 0


def main(argv: Sequence[str], *, out: TextIO, err: TextIO) -> int:
    """Run one subcommand and return its exit code: 0 valid, 1 violations, 2 invalid input."""
    try:
        args = _parser().parse_args(argv)
        if args.command_name == "check-message":
            raw = args.file.read_bytes()
            try:
                text = strip_comments(raw.decode("utf-8"))
            except UnicodeDecodeError:
                return _report(check_message_bytes(raw, commit=str(args.file)), out, err)
            found = check_message(
                text, commit=str(args.file), branch=args.branch, require_pass=args.require_pass
            )
            return _report(found, out, err)
        if args.command_name == "check-range":
            return _report(_check_range(args.repo, args.range, args.branch), out, err)
        return _install_hook(args.dir.resolve(), [p.resolve() for p in args.worktree], out)
    except (InvalidInputError, OSError, ValueError) as error:
        err.write(f"error: {error}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], out=sys.stdout, err=sys.stderr))
