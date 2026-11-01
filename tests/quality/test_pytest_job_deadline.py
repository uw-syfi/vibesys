"""Every pytest CI job dumps the running test before its own timeout cancels it.

The per-test bound cannot see a job that is about to be cancelled: a hang that
starts late in a shard, or a slow test whose bound is longer than the job, is
killed with the runner and never named. The deadline is the job's
``timeout-minutes``, and the dump fires one margin before that.
"""

from __future__ import annotations

import inspect
import json
import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from scripts.pytest_hang_dump import current_nodeid, render_dump, running_test
from scripts.pytest_job_deadline import (
    ARM_COMMAND,
    DUMP_AT_ENV,
    DUMP_MARGIN_S,
    SECONDS_PER_MINUTE,
    STARTED_AT_ENV,
    JobDeadlineError,
    arm_job,
    audit,
    dump_deadline,
    job_timeout_minutes,
    main,
    pytest_job_ids,
    workflow_filename,
    workflow_gaps,
    workflow_jobs,
)

ROOT = Path(__file__).resolve().parents[2]
# Jobs whose cancel has already hidden a hang, plus the other pytest entry points.
# The audit still fails a new pytest job that is not in this set.
REQUIRED_JOBS = {
    ("nightly.yml", "chaos-sweep"),
    ("nightly.yml", "crash-sweep"),
    ("nightly.yml", "full-depth"),
    ("nightly.yml", "real-tiers"),
    ("test.yml", "agent-image"),
    ("test.yml", "classify-failures"),
    ("test.yml", "clients-quality"),
    ("test.yml", "native-evaluator-examples"),
    ("test.yml", "python-guards"),
    ("test.yml", "real-tiers-minimal-container"),
    ("test.yml", "real-tiers-slurm"),
    ("test.yml", "sandbox-linux"),
    ("test.yml", "sandbox-macos"),
    ("test.yml", "seed-exploration"),
    ("test.yml", "test"),
    ("test.yml", "test-impact"),
    ("test.yml", "validate-examples"),
    ("test.yml", "validate-examples-cpu-check"),
}

_ARMED = """
on:
  push:
jobs:
  demo:
    timeout-minutes: 10
    steps:
      - name: Record when this job's cancel clock started
        run: echo "VIBESYS_JOB_STARTED_AT=$(date +%s)" >> "$GITHUB_ENV"
      - uses: actions/checkout@v1
      - name: Pass the pytest cancel deadline
        run: python3 scripts/pytest_job_deadline.py
      - name: Run
        run: uv run python -m pytest -q
"""

_BARE = """
jobs:
  demo:
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v1
      - name: Run
        run: uv run python -m pytest -q
"""


def test_every_pytest_ci_job_passes_its_deadline() -> None:
    assert pytest_job_ids(ROOT) >= REQUIRED_JOBS
    assert audit(ROOT) == []


def test_a_pytest_job_without_the_deadline_is_named() -> None:
    gaps = workflow_gaps("ci.yml", _BARE, set(), {})

    assert gaps
    assert all("demo" in gap for gap in gaps)
    assert any("pytest_job_deadline.py" in gap for gap in gaps)


def test_an_armed_job_is_not_a_gap() -> None:
    assert workflow_gaps("ci.yml", _ARMED, set(), {}) == []


def test_the_job_timeout_is_not_a_step_timeout() -> None:
    workflow = """
jobs:
  demo:
    timeout-minutes: 12
    steps:
      - name: Install
        timeout-minutes: 2
"""

    assert job_timeout_minutes(workflow, "demo") == 12


def test_a_margin_that_fills_the_job_is_a_gap() -> None:
    workflow = """
jobs:
  demo:
    timeout-minutes: 1
    steps:
      - name: Record when this job's cancel clock started
        run: echo "VIBESYS_JOB_STARTED_AT=$(date +%s)" >> "$GITHUB_ENV"
      - uses: actions/checkout@v1
      - name: Pass the pytest cancel deadline
        run: python3 scripts/pytest_job_deadline.py
      - name: Run
        run: uv run python -m pytest -q
"""

    gaps = workflow_gaps("ci.yml", workflow, set(), {})

    assert any("not inside" in gap for gap in gaps)


@pytest.mark.regression
@example(started=1_700_000_000, timeout_minutes=10)
@given(
    started=st.integers(min_value=0, max_value=2_000_000_000),
    timeout_minutes=st.integers(
        min_value=(DUMP_MARGIN_S // SECONDS_PER_MINUTE) + 1,
        max_value=24 * SECONDS_PER_MINUTE,
    ),
)
def test_the_dump_is_one_margin_before_the_job_cancel(started: int, timeout_minutes: int) -> None:
    # The reported hang is a 10 minute shard cancel (the `test` job) that
    # arrives before the per-test bound. The deadline is that cancel, minus
    # the margin, for every timeout the margin fits inside.
    cancel = started + timeout_minutes * SECONDS_PER_MINUTE
    deadline = dump_deadline(started, timeout_minutes)

    assert deadline == cancel - DUMP_MARGIN_S
    assert started < deadline < cancel


def test_the_dump_names_the_running_test_and_every_thread() -> None:
    frame = inspect.currentframe()
    assert frame is not None
    outer = current_nodeid()
    with running_test("tests/quality/test_pytest_job_deadline.py::test_one"):
        assert current_nodeid() == "tests/quality/test_pytest_job_deadline.py::test_one"
        text = render_dump(current_nodeid(), {7: frame, 8: frame}, {7: "MainThread", 8: "worker"})

    assert "running test: tests/quality/test_pytest_job_deadline.py::test_one" in text
    assert "Thread MainThread (7):" in text
    assert "Thread worker (8):" in text
    assert "test_the_dump_names_the_running_test_and_every_thread" in text
    assert current_nodeid() == outer


def test_the_dump_says_when_no_test_is_running() -> None:
    text = render_dump(None, {}, {})

    assert "running test: (no test is running)" in text


class _Caller(StrEnum):
    """How the deadline script is reached. GitHub's env shape differs for each."""

    DIRECT = "direct"
    MATRIX = "matrix"
    REUSABLE = "reusable"
    COMPOSITE = "composite"


@dataclass(frozen=True)
class _GithubJob:
    """One generated Actions environment and the workflow tree it addresses."""

    caller: _Caller
    filename: str
    display_name: str
    job_id: str
    job_display: str
    minutes: int
    started: int
    owner_repo: str
    ref: str


_IDENT = st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True)
_FILENAME = _IDENT.map(lambda name: f"{name}.yml")
_DISPLAY = st.from_regex(r"[A-Za-z][A-Za-z0-9 ()._-]{0,20}", fullmatch=True)
_REF = st.one_of(
    st.just("refs/heads/main"),
    st.just("refs/pull/3394/merge"),
    st.just("refs/heads/feature@with-at"),
    st.from_regex(r"refs/tags/v[0-9]\.[0-9]\.[0-9]", fullmatch=True),
    st.from_regex(r"[0-9a-f]{7,40}", fullmatch=True),
)
# The reported failure: workflow display name Tests, file test.yml, check run `test (1)`.
_REPORTED = _GithubJob(
    caller=_Caller.MATRIX,
    filename="test.yml",
    display_name="Tests",
    job_id="test",
    job_display="test (1)",
    minutes=10,
    started=1_700_000_000,
    owner_repo="example/repo",
    ref="refs/pull/3394/merge",
)
_DECOY_MINUTES = 99
_GITHUB_ENV_KEYS = (
    "GITHUB_WORKFLOW",
    "GITHUB_WORKFLOW_REF",
    "GITHUB_JOB",
    "GITHUB_WORKSPACE",
    "GITHUB_ENV",
    "GITHUB_ACTION_PATH",
    STARTED_AT_ENV,
)


def _distinct_display(name: str, filename: str) -> bool:
    folded = name.casefold()
    if folded == filename.casefold() or folded.endswith((".yml", ".yaml")):
        return False
    return name not in {".", ".."} and not name.startswith(".")


def _quote(value: str) -> str:
    return json.dumps(value)


def _job_yaml(job: _GithubJob, *, matrix: bool) -> str:
    strategy = "    strategy:\n      matrix:\n        shard: [1]\n" if matrix else ""
    return (
        f"  other:\n"
        f"    timeout-minutes: {_DECOY_MINUTES}\n"
        f"    steps:\n"
        f"      - run: echo other\n"
        f"  {job.job_id}:\n"
        f"    name: {_quote(job.job_display)}\n"
        f"    timeout-minutes: {job.minutes}\n"
        f"{strategy}"
        f"    steps:\n"
        f"      - name: Pass the pytest cancel deadline\n"
        f"        run: python3 scripts/pytest_job_deadline.py\n"
    )


def _write_workflow(path: Path, display_name: str, jobs: str, trigger: str) -> None:
    path.write_text(
        f"name: {_quote(display_name)}\non:\n  {trigger}:\njobs:\n{jobs}",
        encoding="utf-8",
    )


def _write_decoy(workflows: Path, job: _GithubJob) -> None:
    """A file named the display name, so using GITHUB_WORKFLOW as a path arms the wrong timeout."""
    if not _distinct_display(job.display_name, job.filename):
        return
    decoy = workflows / job.display_name
    if decoy.name in {job.filename, "caller.yml"}:
        return
    _write_workflow(
        decoy,
        "Decoy",
        f"  {job.job_id}:\n    timeout-minutes: {_DECOY_MINUTES}\n",
        "push",
    )


def _env_for(job: _GithubJob, filename: str, workflow_name: str) -> dict[str, str]:
    return {
        "GITHUB_WORKFLOW": workflow_name,
        "GITHUB_WORKFLOW_REF": f"{job.owner_repo}/.github/workflows/{filename}@{job.ref}",
        "GITHUB_JOB": job.job_id,
        STARTED_AT_ENV: str(job.started),
    }


def _write_called(workflows: Path, job: _GithubJob, *, matrix: bool) -> None:
    _write_workflow(
        workflows / job.filename, "Called suite", _job_yaml(job, matrix=matrix), "workflow_call"
    )


def _write_direct(workflows: Path, job: _GithubJob) -> dict[str, str]:
    _write_called(workflows, job, matrix=job.caller is _Caller.MATRIX)
    return _env_for(job, job.filename, job.display_name)


def _write_reusable(workflows: Path, job: _GithubJob) -> dict[str, str]:
    _write_workflow(
        workflows / "caller.yml",
        job.display_name,
        f"  call:\n    name: Call the suite\n    uses: ./.github/workflows/{job.filename}\n",
        "push",
    )
    _write_called(workflows, job, matrix=False)
    # Inside the called workflow, GITHUB_WORKFLOW is the caller's display name.
    return _env_for(job, job.filename, job.display_name)


def _write_composite(root: Path, workflows: Path, job: _GithubJob) -> dict[str, str]:
    action = root / ".github" / "actions" / "arm-deadline"
    action.mkdir(parents=True)
    (action / "action.yml").write_text(
        "name: Arm the deadline\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - run: python3 scripts/pytest_job_deadline.py\n"
        "      shell: bash\n",
        encoding="utf-8",
    )
    _write_workflow(
        workflows / job.filename,
        job.display_name,
        (
            f"  other:\n"
            f"    timeout-minutes: {_DECOY_MINUTES}\n"
            f"    steps:\n"
            f"      - run: echo other\n"
            f"  {job.job_id}:\n"
            f"    name: {_quote(job.job_display)}\n"
            f"    timeout-minutes: {job.minutes}\n"
            f"    steps:\n"
            f"      - uses: ./.github/actions/arm-deadline\n"
        ),
        "push",
    )
    env = _env_for(job, job.filename, job.display_name)
    env["GITHUB_ACTION_PATH"] = str(action)
    return env


def _write_fixture(root: Path, job: _GithubJob) -> dict[str, str]:
    if root.exists():
        shutil.rmtree(root)
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    _write_decoy(workflows, job)
    if job.caller is _Caller.REUSABLE:
        return _write_reusable(workflows, job)
    if job.caller is _Caller.COMPOSITE:
        return _write_composite(root, workflows, job)
    return _write_direct(workflows, job)


@st.composite
def _github_jobs(draw: st.DrawFn) -> _GithubJob:
    filename = draw(_FILENAME.filter(lambda name: name != "caller.yml"))
    display_name = draw(_DISPLAY.filter(lambda name: _distinct_display(name, filename)))
    job_id = draw(_IDENT.filter(lambda name: name not in {"other", "call", "decoy"}))
    job_display = draw(
        st.one_of(
            st.just(f"{job_id} (1)"),
            _DISPLAY.filter(lambda name: name != job_id),
        )
    )
    return _GithubJob(
        caller=draw(st.sampled_from(list(_Caller))),
        filename=filename,
        display_name=display_name,
        job_id=job_id,
        job_display=job_display,
        minutes=draw(st.integers(min_value=2, max_value=40)),
        started=draw(st.integers(min_value=0, max_value=2_000_000_000)),
        owner_repo=f"{draw(_IDENT)}/{draw(_IDENT)}",
        ref=draw(_REF),
    )


def _assert_armed(env: dict[str, str], workspace: Path, env_file: Path, job: _GithubJob) -> None:
    env_file.write_text("KEEP=1\n", encoding="utf-8")
    warning = arm_job(env, workspace, env_file)
    deadline = dump_deadline(job.started, job.minutes)

    assert warning is None
    assert env["GITHUB_WORKFLOW"] != job.filename
    assert env["GITHUB_JOB"] == job.job_id
    assert env["GITHUB_JOB"] != job.job_display
    assert workflow_filename(env["GITHUB_WORKFLOW_REF"]) == job.filename
    assert env_file.read_text(encoding="utf-8") == f"KEEP=1\n{DUMP_AT_ENV}={deadline}\n"
    assert deadline != dump_deadline(job.started, _DECOY_MINUTES)


@pytest.mark.regression
@example(job=_REPORTED)
@given(job=_github_jobs())
def test_the_deadline_comes_from_the_workflow_ref_and_the_job_id(job: _GithubJob) -> None:
    # Run 39736731918, job `test (1)`: GITHUB_WORKFLOW was the display name
    # `Tests`, so the script opened `.github/workflows/Tests` and failed the job.
    # The file is the one GITHUB_WORKFLOW_REF names; the job id is GITHUB_JOB,
    # including when the check run is `test (1)`, a reusable workflow is running
    # under its caller's name, or a composite action sets GITHUB_ACTION_PATH.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = _write_fixture(root, job)
        _assert_armed(env, root, root / "github.env", job)


def _display_name(workflow_text: str, filename: str) -> str:
    for line in workflow_text.splitlines():
        if line.startswith("name:"):
            value = line.split(":", 1)[1].strip().strip("'\"")
            if value:
                return value
    return f".github/workflows/{filename}"


def test_every_workflow_file_that_calls_the_script_arms_from_its_ref(tmp_path: Path) -> None:
    files = sorted(
        path
        for path in (ROOT / ".github" / "workflows").glob("*.yml")
        if "python3 scripts/pytest_job_deadline.py" in path.read_text(encoding="utf-8")
    )

    assert {path.name for path in files} >= {"test.yml", "nightly.yml"}
    for path in files:
        text = path.read_text(encoding="utf-8")
        display = _display_name(text, path.name)
        assert display != path.name
        jobs = [job for job, body in workflow_jobs(text).items() if ARM_COMMAND in body]
        assert jobs, path.name
        for job in jobs:
            _assert_real_job_arms(tmp_path, path, display, job, text)


def _assert_real_job_arms(tmp_path: Path, path: Path, display: str, job: str, text: str) -> None:
    started = 1_700_000_000
    minutes = job_timeout_minutes(text, job)
    env = {
        "GITHUB_WORKFLOW": display,
        "GITHUB_WORKFLOW_REF": f"example/repo/.github/workflows/{path.name}@refs/heads/main",
        "GITHUB_JOB": job,
        STARTED_AT_ENV: str(started),
    }
    env_file = tmp_path / f"{path.stem}-{job}"
    env_file.write_text("KEEP=1\n", encoding="utf-8")
    warning = arm_job(env, ROOT, env_file)

    assert warning is None
    assert env_file.read_text(encoding="utf-8") == (
        f"KEEP=1\n{DUMP_AT_ENV}={dump_deadline(started, minutes)}\n"
    )
    # A matrix cell is check run `job (1)` while GITHUB_JOB stays the job id.
    matrix_env = dict(env)
    matrix_env["GITHUB_JOB"] = f"{job} (1)"
    matrix_file = tmp_path / f"{path.stem}-{job}-display"
    matrix_file.write_text("KEEP=1\n", encoding="utf-8")
    matrix_warning = arm_job(matrix_env, ROOT, matrix_file)

    assert matrix_warning is not None
    assert matrix_warning.count("\n") == 0
    assert "skipping the hang dump" in matrix_warning
    assert matrix_file.read_text(encoding="utf-8") == "KEEP=1\n"


class _Gap(StrEnum):
    MISSING_REF = "missing-ref"
    DISPLAY_NAME_AS_REF = "display-as-ref"
    MISSING_FILE = "missing-file"
    JOB_DISPLAY_AS_ID = "job-display-as-id"
    BAD_START = "bad-start"
    MISSING_ENV_FILE = "missing-env-file"
    NO_TIMEOUT = "no-timeout"


def _gap_fixture(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    _write_workflow(
        workflows / "test.yml",
        "Tests",
        "  test:\n    timeout-minutes: 10\n    steps:\n      - run: python3 scripts/pytest_job_deadline.py\n",
        "push",
    )
    _write_workflow(
        workflows / "Tests",
        "Decoy",
        "  test:\n    timeout-minutes: 99\n",
        "push",
    )
    _write_workflow(
        workflows / "bare.yml",
        "Bare",
        "  test:\n    steps:\n      - run: python3 scripts/pytest_job_deadline.py\n",
        "push",
    )


def _gap_env(gap: _Gap) -> dict[str, str]:
    env = {
        "GITHUB_WORKFLOW": "Tests",
        "GITHUB_WORKFLOW_REF": "example/repo/.github/workflows/test.yml@refs/heads/main",
        "GITHUB_JOB": "test",
        STARTED_AT_ENV: "1700000000",
    }
    if gap is _Gap.MISSING_REF:
        del env["GITHUB_WORKFLOW_REF"]
    elif gap is _Gap.DISPLAY_NAME_AS_REF:
        env["GITHUB_WORKFLOW_REF"] = "Tests"
    elif gap is _Gap.MISSING_FILE:
        env["GITHUB_WORKFLOW_REF"] = "example/repo/.github/workflows/missing.yml@refs/heads/main"
    elif gap is _Gap.JOB_DISPLAY_AS_ID:
        env["GITHUB_JOB"] = "test (1)"
    elif gap is _Gap.BAD_START:
        env[STARTED_AT_ENV] = "not-a-time"
    elif gap is _Gap.NO_TIMEOUT:
        env["GITHUB_WORKFLOW_REF"] = "example/repo/.github/workflows/bare.yml@refs/heads/main"
    return env


@pytest.mark.regression
@example(gap=_Gap.DISPLAY_NAME_AS_REF)
@example(gap=_Gap.MISSING_REF)
@given(gap=st.sampled_from(list(_Gap)))
def test_a_missing_or_unparsable_deadline_skips_without_arming(gap: _Gap) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _gap_fixture(root)
        env_file = root / "github.env"
        env_file.write_text("KEEP=1\n", encoding="utf-8")
        env_path = None if gap is _Gap.MISSING_ENV_FILE else env_file

        warning = arm_job(_gap_env(gap), root, env_path)

        assert warning is not None
        assert warning.count("\n") == 0
        assert warning.startswith("pytest job deadline: ")
        assert warning.endswith("skipping the hang dump")
        assert env_file.read_text(encoding="utf-8") == "KEEP=1\n"
        assert DUMP_AT_ENV not in env_file.read_text(encoding="utf-8")


def test_main_arms_the_reported_job_and_skips_an_unusable_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "reported"
    env = _write_fixture(root, _REPORTED)
    env_file = root / "github.env"
    env_file.write_text("", encoding="utf-8")
    _install_env(monkeypatch, env, root, env_file)

    assert main() == 0
    assert capsys.readouterr().err == ""
    assert env_file.read_text(encoding="utf-8") == (
        f"{DUMP_AT_ENV}={dump_deadline(_REPORTED.started, _REPORTED.minutes)}\n"
    )

    for key in _GITHUB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITHUB_WORKFLOW", "Tests")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(root))
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    monkeypatch.setenv("GITHUB_JOB", "test")
    monkeypatch.setenv(STARTED_AT_ENV, str(_REPORTED.started))

    assert main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "skipping the hang dump" in captured.err
    assert env_file.read_text(encoding="utf-8") == (
        f"{DUMP_AT_ENV}={dump_deadline(_REPORTED.started, _REPORTED.minutes)}\n"
    )


def _install_env(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], workspace: Path, env_file: Path
) -> None:
    for key in _GITHUB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    monkeypatch.setenv("GITHUB_ENV", str(env_file))


def test_workflow_filename_rejects_a_display_name() -> None:
    with pytest.raises(JobDeadlineError):
        workflow_filename("Tests")
