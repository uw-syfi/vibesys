"""The `commit` skill's checker: what it accepts, what it names, and how it installs its hook.

The script is a standalone program, so its public surface is its command line (`main`) plus the
pure checks it exposes (`check_message`, `check_commits`, `parse_message`). Real git is used
where git is the point: trailer parsing is compared with `git interpret-trailers`, `check-range`
reads real commits, and the installed hook runs under a real `git commit`.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "commit_schema.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("commit_schema_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their annotations through this
    spec.loader.exec_module(module)
    return module


script = _load_script()
GIT = shutil.which("git") or "git"
PLAN_BRANCH = "vibesys-work/demo/plan-004"
GOOD = "feat(decode): add a fused sampler\n\nSampling re-read the logits.\n\nChecks: pass\n"


def cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = script.main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def rules(text: str, **kwargs: object) -> set[str]:
    return {v.rule for v in script.check_message(text, commit="c", **kwargs)}


# --- strategies --------------------------------------------------------------------------------

VERBS = ("add", "fix", "remove", "rename", "extract", "reduce", "cache", "batch", "fuse", "split")
WORD = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)
SCOPE = st.from_regex(r"[A-Za-z0-9._/-]{1,12}", fullmatch=True)


@st.composite
def subjects(draw: st.DrawFn) -> str:
    """An imperative subject that fits the first line with the longest type and scope."""
    words = draw(st.lists(WORD, max_size=4))
    return " ".join([draw(st.sampled_from(VERBS)), *words])


BODY_LINE = st.text(alphabet="abcdefghijklmnopqrstuvwxyz 0123456789", max_size=72).map(str.strip)


@st.composite
def valid_messages(draw: st.DrawFn) -> tuple[str, str | None]:
    """A schema-valid full message and the plan branch it needs (or ``None``)."""
    kind = draw(st.sampled_from(script.TYPES))
    scope = draw(st.none() | SCOPE)
    header = f"{kind}{f'({scope})' if scope else ''}: {draw(subjects())}"
    lines = [header]
    body = [line for line in draw(st.lists(BODY_LINE, max_size=5)) if line]
    if body:
        lines += ["", *body]
    trailers = [f"Checks: {draw(st.sampled_from(script.CHECKS_VALUES))}"]
    branch = None
    if draw(st.booleans()):
        number = draw(st.integers(0, 999))
        branch = f"vibesys-work/{draw(WORD)}/plan-{number:03d}"
        trailers.append(f"Plan: {number:03d}")
    return "\n".join([*lines, "", *trailers]) + "\n", branch


# --- the schema --------------------------------------------------------------------------------


@given(valid_messages())
def test_every_generated_valid_message_has_no_violation(case: tuple[str, str | None]) -> None:
    text, branch = case
    assert rules(text, branch=branch) == set()


@given(valid_messages())
def test_dropping_the_checks_trailer_is_always_named(case: tuple[str, str | None]) -> None:
    text, branch = case
    without = "\n".join(line for line in text.splitlines() if not line.startswith("Checks:"))
    assert "checks-missing" in rules(without, branch=branch)


@given(valid_messages(), st.sampled_from(("fail", "not-run")))
def test_a_candidate_must_say_pass_whatever_else_is_right(
    case: tuple[str, str | None], value: str
) -> None:
    text, branch = case
    text = text.replace("Checks: pass", f"Checks: {value}")
    text = text.replace("Checks: fail", f"Checks: {value}").replace(
        "Checks: not-run", f"Checks: {value}"
    )
    assert rules(text, branch=branch, require_pass=True) == {"checks-not-pass"}


@given(valid_messages())
def test_a_long_first_line_is_named_and_a_short_one_is_not(case: tuple[str, str | None]) -> None:
    text, branch = case
    header, rest = text.split("\n", 1)
    assert "subject-length" not in rules(text, branch=branch)
    assert "subject-length" in rules(header + " " + "x" * 80 + "\n" + rest, branch=branch)


@given(valid_messages(), st.integers(1, 999))
def test_a_plan_trailer_must_equal_the_branch_number(
    case: tuple[str, str | None], offset: int
) -> None:
    text, _ = case
    body = "\n".join(line for line in text.splitlines() if not line.startswith("Plan:"))
    branch = "vibesys-work/demo/plan-010"
    assert "plan-missing" in rules(body, branch=branch)
    assert rules(body + f"\nPlan: {10:03d}\n", branch=branch) == set()
    assert "plan-mismatch" in rules(body + f"\nPlan: {10 + offset}\n", branch=branch)
    assert "plan-missing" not in rules(body)  # off a plan branch the trailer is optional


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Added caching.\n", "header-format"),
        ("Feature: x\n\nChecks: pass\n", "header-format"),
        ("feat:x\n\nChecks: pass\n", "header-format"),
        ("wip!: x\n", "header-format"),
        ("feature: add x\n\nChecks: pass\n", "type-unknown"),
        ("feat: \n\nChecks: pass\n", "subject-empty"),
        ("feat: add x.\n\nChecks: pass\n", "subject-period"),
        ("feat: added x\n\nChecks: pass\n", "subject-imperative"),
        ("feat: adding x\n\nChecks: pass\n", "subject-imperative"),
        ("feat: adds x\n\nChecks: pass\n", "subject-imperative"),
        ("feat: add x\nChecks: pass\n", "blank-after-subject"),
        ("fix: handle empty input\n", "checks-missing"),
        ("fix: handle empty input\n\nbody\nChecks: pass\n", "checks-missing"),
        ("fix: handle x\n\nChecks: ok\n", "checks-value"),
        ("fix: handle x\n\nChecks: pass\nChecks: pass\n", "checks-duplicate"),
        ("fix: handle x\n\nChecks: pass\nPlan: x\n", "plan-value"),
        ("fix: handle x\n\n" + "w" * 73 + "\n\nChecks: pass\n", "body-line-length"),
        ("", "message-empty"),
        ("  \n\n", "message-empty"),
        ("fix: handle x\x00\n\nChecks: pass\n", "encoding"),
        ("wip:\n", "wip-note-empty"),
        ("wip: \n", "wip-note-empty"),
    ],
)
def test_a_bad_message_names_its_rule_and_the_fix(message: str, expected: str) -> None:
    found = script.check_message(message, commit="abc123")
    assert expected in {v.rule for v in found}
    for violation in found:
        text = str(violation)
        assert text.startswith("abc123: ")
        assert violation.rule in text
        assert "Fix: " in text


def test_words_that_end_like_a_verb_form_but_are_imperative_pass() -> None:
    for subject in ("speed up the loop", "pass the flag", "process requests", "focus the cache"):
        assert rules(f"perf: {subject}\n\nChecks: pass\n") == set(), subject


def test_urls_and_code_may_exceed_the_line_limit() -> None:
    long = "x" * 80
    body = f"See https://example.com/{long}\n\n    {long}\n\n```\n{long}\n```\n"
    assert rules(f"fix: handle x\n\n{body}\nChecks: pass\n") == set()


def test_an_unrecognised_leading_paragraph_does_not_become_a_trailer_block() -> None:
    message = script.parse_message("fix: x\n\nChecks: pass")
    assert [(t.key, t.value) for t in message.trailers] == [("Checks", "pass")]
    assert script.parse_message("fix: x\nChecks: pass").trailers == ()


# --- wip ---------------------------------------------------------------------------------------

NOTE = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ,.0123456789", min_size=1).filter(str.strip)


@given(NOTE, st.none() | SCOPE, st.text(max_size=300).filter(lambda t: "\x00" not in t))
def test_a_wip_commit_needs_only_its_first_line_whatever_follows(
    note: str, scope: str | None, rest: str
) -> None:
    header = f"wip{f'({scope})' if scope else ''}: {note.strip()}"
    assert rules(f"{header}\n{rest}") == set()


@given(NOTE)
def test_a_wip_commit_is_never_a_candidate(note: str) -> None:
    assert rules(f"wip: {note.strip()}\n", require_pass=True) == {"wip-not-measurable"}


def test_wip_ignores_the_length_and_imperative_rules_and_needs_no_trailers() -> None:
    assert rules("wip: tried adding a very long note " + "x" * 100 + ".\n") == set()
    assert rules("wip(kernel): half-done tiling", branch=PLAN_BRANCH) == set()


# --- never crashes -----------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(
    raw=st.binary(max_size=400),
    require_pass=st.booleans(),
    branch=st.none() | st.just(PLAN_BRANCH),
)
def test_fuzzing_message_bytes_gives_exit_code_0_1_or_2(
    tmp_path: Path, *, raw: bytes, require_pass: bool, branch: str | None
) -> None:
    file = tmp_path / "MSG"
    file.write_bytes(raw)
    argv = ["check-message", str(file)]
    argv += ["--require-pass"] if require_pass else []
    argv += ["--branch", branch] if branch else []
    code, _, err = cli(*argv)
    assert code in (0, 1, 2)
    assert (code == 0) == (err == "")


@given(st.text(max_size=300))
def test_checking_arbitrary_text_never_raises_and_every_violation_is_complete(text: str) -> None:
    for violation in script.check_message(text, commit="c", branch=PLAN_BRANCH, require_pass=True):
        assert violation.rule
        assert violation.problem
        assert violation.fix


def test_unusable_input_exits_2(tmp_path: Path) -> None:
    assert cli("check-message", str(tmp_path / "missing"))[0] == 2
    assert cli("check-range", "nodots")[0] == 2
    assert cli("no-such-command")[0] == 2


def test_git_comment_lines_and_the_scissors_tail_are_ignored_by_check_message(
    tmp_path: Path,
) -> None:
    file = tmp_path / "MSG"
    file.write_text(
        f"# header comment\n{GOOD}# ------------------------ >8 ------------------------\nx\n"
    )
    assert cli("check-message", str(file))[0] == 0


# --- trailers against git ----------------------------------------------------------------------


def git(cwd: Path, *argv: str, stdin: str | None = None, check: bool = True) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"], "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
    }  # fmt: skip
    done = subprocess.run(  # noqa: S603  # LW-100376; fixed git argv in a temp repository.
        [GIT, "-C", str(cwd), *argv], check=False, capture_output=True, text=True, env=env,
        input=stdin,
    )  # fmt: skip
    # > The point is to agree with real git; a Fake of git would test the Fake.
    assert not check or done.returncode == 0, f"git {argv} failed: {done.stderr}"
    return done.stdout if check else f"{done.returncode}\n{done.stdout}{done.stderr}"


LINE = st.one_of(
    st.from_regex(r"[A-Za-z][A-Za-z0-9-]{0,8}: [a-z0-9]{1,8}", fullmatch=True),
    st.from_regex(r"[a-z][a-z0-9 ]{0,12}", fullmatch=True),
    st.just("Signed-off-by: a b"),
    st.from_regex(r"[ \t][a-z]{1,6}", fullmatch=True),
)


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("repo")
    git(root, "init", "-q", "-b", "main")
    return root


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None, max_examples=60
)
@given(st.lists(st.lists(LINE, min_size=1, max_size=5), min_size=1, max_size=4))
def test_the_trailer_block_matches_git_interpret_trailers(
    repo: Path, paragraphs: list[list[str]]
) -> None:
    text = "fix: title\n\n" + "\n\n".join("\n".join(p) for p in paragraphs) + "\n"
    expected = git(repo, "interpret-trailers", "--parse", stdin=text).splitlines()
    ours = [f"{t.key}: {t.value}" for t in script.parse_message(text).trailers]
    assert ours == expected, text


# --- check-range -------------------------------------------------------------------------------


class Repo:
    """A real repository that commits with stated messages."""

    def __init__(self, root: Path) -> None:
        self.root = root
        git(root, "init", "-q", "-b", "main")
        (root / "base").write_text("base")
        git(root, "add", "base")
        git(root, "commit", "-q", "-m", "base commit")
        self.base = self.head()

    def head(self) -> str:
        return git(self.root, "rev-parse", "HEAD").strip()

    def commit(self, message: str, *, verify: bool = True) -> str:
        path = self.root / f"f{len(list(self.root.iterdir()))}"
        path.write_text(message)
        git(self.root, "add", path.name)
        args = ["commit", "-q", "-F", "-"] + ([] if verify else ["--no-verify"])
        git(self.root, *args, stdin=message)
        return self.head()

    def range(self, spec: str, *extra: str) -> tuple[int, str, str]:
        return cli("check-range", spec, "--repo", str(self.root), *extra)


@pytest.fixture
def repo_of_commits(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_check_range_accepts_wip_commits_below_a_full_passing_head(repo_of_commits: Repo) -> None:
    r = repo_of_commits
    r.commit("wip: first idea")
    r.commit("wip(kernel): half-done tiling\n\nanything goes here, no trailers")
    r.commit("perf: fuse the sampler\n\nChecks: not-run\n")
    r.commit(GOOD)
    assert r.range(f"{r.base}..HEAD")[0] == 0


def test_check_range_rejects_a_wip_head_and_a_head_that_did_not_pass(repo_of_commits: Repo) -> None:
    r = repo_of_commits
    r.commit(GOOD)
    sha = r.commit("wip: after the good one")
    code, _, err = r.range(f"{r.base}..HEAD")
    assert code == 1
    assert f"{sha[:12]}: wip-not-measurable" in err
    assert "Fix: make a real" in err
    r.commit("fix: handle x\n\nChecks: fail\n")
    assert "checks-not-pass" in r.range(f"{r.base}..HEAD")[2]


def test_check_range_names_the_commit_that_breaks_a_rule(repo_of_commits: Repo) -> None:
    r = repo_of_commits
    r.commit("Added a thing.")
    sha = r.commit("feat: fix it\n\n" + "w" * 80 + "\n\nChecks: pass\n")
    r.commit(GOOD)
    code, _, err = r.range(f"{r.base}..HEAD")
    assert code == 1
    assert "header-format" in err
    assert f"{sha[:12]}: body-line-length" in err


def test_check_range_enforces_the_plan_trailer_on_a_plan_branch(repo_of_commits: Repo) -> None:
    r = repo_of_commits
    r.commit(GOOD)
    assert r.range(f"{r.base}..HEAD", "--branch", PLAN_BRANCH)[2].count("plan-missing") == 1
    r.commit(GOOD.replace("Checks: pass", "Checks: pass\nPlan: 004"))
    code, _, err = r.range(f"{r.base}..HEAD", "--branch", PLAN_BRANCH)
    assert code == 1
    assert err.count("plan-missing") == 1  # the older commit still lacks it
    assert r.range("HEAD~1..HEAD", "--branch", PLAN_BRANCH)[0] == 0


def test_check_range_reads_the_plan_from_the_branch_named_in_the_range(
    repo_of_commits: Repo,
) -> None:
    r = repo_of_commits
    git(r.root, "switch", "-q", "-c", PLAN_BRANCH)
    r.commit(GOOD)
    assert "plan-missing" in r.range(f"{r.base}..{PLAN_BRANCH}")[2]


def test_check_range_needs_a_real_nonempty_range(repo_of_commits: Repo) -> None:
    r = repo_of_commits
    assert r.range(f"{r.base}..HEAD")[0] == 2  # empty
    assert r.range("nothere..HEAD")[0] == 2
    assert r.range("HEAD")[0] == 2


# --- install-hook ------------------------------------------------------------------------------


class Optimization:
    """A repository with a user hook and one linked worktree."""

    def __init__(self, root: Path) -> None:
        self.repo = Repo(root)
        self.root = root
        self.worktree = root.parent / f"{root.name}-work" / "main"
        git(root, "worktree", "add", "-q", "-b", "vibesys/demo", str(self.worktree))
        hooks = root / ".git" / "hooks"
        self.log = root / "hook.log"
        for name in ("pre-commit", "commit-msg"):
            hook = hooks / name
            hook.write_text(f'#!/bin/sh\necho {name} "$@" >> {self.log}\nexit 0\n')
            hook.chmod(0o755)

    def install(self) -> tuple[int, str, str]:
        return cli("install-hook", "--worktree", str(self.worktree))

    def commit(self, cwd: Path, message: str) -> str:
        (cwd / "x").write_text(message)
        git(cwd, "add", "x")
        return git(cwd, "commit", "-q", "-F", "-", stdin=message, check=False)


@pytest.fixture
def optimization(tmp_path: Path) -> Optimization:
    return Optimization(tmp_path)


def test_the_hook_rejects_a_bad_message_in_the_optimizations_worktree(
    optimization: Optimization,
) -> None:
    assert optimization.install()[0] == 0
    result = optimization.commit(optimization.worktree, "Added stuff.")
    assert result.startswith("1\n")
    assert "header-format" in result
    assert "Fix:" in result
    assert optimization.commit(optimization.worktree, GOOD).startswith("0\n")


def test_the_hook_accepts_a_wip_checkpoint(optimization: Optimization) -> None:
    optimization.install()
    assert optimization.commit(optimization.worktree, "wip: trying the other layout").startswith(
        "0"
    )


def test_the_hook_chains_to_the_repositorys_own_hooks(optimization: Optimization) -> None:
    optimization.install()
    optimization.commit(optimization.worktree, GOOD)
    logged = optimization.log.read_text()
    assert "commit-msg" in logged
    assert "pre-commit" in logged  # the user's hooks still ran


def test_an_existing_hook_that_fails_still_blocks_the_commit(optimization: Optimization) -> None:
    hook = optimization.root / ".git" / "hooks" / "commit-msg"
    hook.write_text("#!/bin/sh\nexit 1\n")
    optimization.install()
    assert optimization.commit(optimization.worktree, GOOD).startswith("1\n")


def test_the_users_own_checkout_is_left_alone(optimization: Optimization) -> None:
    before = (optimization.root / ".git" / "hooks" / "commit-msg").read_text()
    optimization.install()
    assert (optimization.root / ".git" / "hooks" / "commit-msg").read_text() == before
    assert git(optimization.root, "config", "--get", "core.hooksPath", check=False).startswith(
        "1\n"
    )
    result = optimization.commit(optimization.root, "Not a conventional message")
    assert result.startswith("0\n")  # the schema does not apply there
    assert "commit-msg" in optimization.log.read_text()  # but the user's hook still runs


def test_install_hook_is_idempotent_and_keeps_chaining(optimization: Optimization) -> None:
    assert optimization.install()[0] == 0
    assert optimization.install()[0] == 0
    optimization.commit(optimization.worktree, GOOD)
    assert "commit-msg" in optimization.log.read_text()
    assert optimization.commit(optimization.worktree, "bad").startswith("1\n")


def test_the_hook_requires_the_plan_trailer_on_a_plan_branch(optimization: Optimization) -> None:
    optimization.install()
    git(optimization.worktree, "switch", "-q", "-c", PLAN_BRANCH)
    assert "plan-missing" in optimization.commit(optimization.worktree, GOOD)
    planned = GOOD.replace("Checks: pass", "Checks: pass\nPlan: 004")
    assert optimization.commit(optimization.worktree, planned).startswith("0\n")


def test_install_hook_needs_a_worktree(tmp_path: Path) -> None:
    Repo(tmp_path)
    assert cli("install-hook")[0] == 2
    assert cli("install-hook", "--worktree", str(tmp_path / "missing"))[0] == 2


def test_the_generated_hooks_live_in_the_git_directory(optimization: Optimization) -> None:
    optimization.install()
    generated = optimization.root / ".git" / "vibesys-hooks"
    assert any(generated.rglob("commit-msg"))
    assert not (optimization.root / ".vibesys-state").exists()
