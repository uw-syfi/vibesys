"""Pass a pytest session the absolute time its CI job will be cancelled.

The workflow's ``timeout-minutes`` is the only copy of that limit. The first
step of a pytest job records when the cancel clock started; the next step
after checkout runs this script. It locates the workflow file from
``GITHUB_WORKFLOW_REF`` (``<owner>/<repo>/.github/workflows/<file>@<ref>``)
and the job from ``GITHUB_JOB``, reads that job's ``timeout-minutes``, and
appends ``VIBESYS_PYTEST_DUMP_AT`` to ``GITHUB_ENV``. ``GITHUB_WORKFLOW`` is
the workflow's display name, not a file, so it is not a path. The hang-dump
plugin fires ``DUMP_MARGIN_S`` before the runner cancels the job.

A missing or unparsable deadline logs one warning and skips the dump. The
dump is a diagnostic and must not fail the job.

Usage (from a workflow step, after checkout):

    python3 scripts/pytest_job_deadline.py
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

SECONDS_PER_MINUTE = 60
DUMP_MARGIN_S = 60
STARTED_AT_ENV = "VIBESYS_JOB_STARTED_AT"
DUMP_AT_ENV = "VIBESYS_PYTEST_DUMP_AT"
PYTEST_PLUGIN = "scripts.pytest_hang_dump"
ARM_COMMAND = "python3 scripts/pytest_job_deadline.py"
RECORD_SNIPPET = f"{STARTED_AT_ENV}=$(date +%s)"
_ARM_SCRIPT = "scripts/pytest_job_deadline.py"
_JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
_TIMEOUT = re.compile(r"^    timeout-minutes:\s*(\d+)\s*(?:#.*)?$")
_PYTEST = re.compile(r"(?<![\w.-])pytest(?![\w.-])")
_GROUP = re.compile(r"run-checks\s+--group\s+([A-Za-z0-9_-]+)")
_MATRIX_GROUP = re.compile(r"run-checks\s+--group\s+\$\{\{\s*matrix\.group\s*\}\}")
_PATH = re.compile(r"(?<!\S)(?:\./)?(?P<path>(?:[\w.-]+/)+[\w.-]+\.(?:py|sh))")
# GITHUB_WORKFLOW_REF is `<owner>/<repo>/.github/workflows/<file>@<ref>`.
# The file is one path segment; the ref may itself contain `@`.
_WORKFLOW_REF_FILE = re.compile(r"\.github/workflows/(?P<name>[^/@]+\.ya?ml)@(?P<ref>\S+)")


class JobDeadlineError(ValueError):
    """The workflow does not name a job timeout the deadline can be read from."""


def dump_deadline(started_at: int, timeout_minutes: int) -> int:
    """Unix time at which the session should dump stacks for one job.

    ``started_at`` is when the job's cancel clock started, and
    ``timeout_minutes`` is that job's ``timeout-minutes``. The result is
    ``DUMP_MARGIN_S`` before the runner cancels the job.
    """
    if started_at < 0:
        message = f"job start {started_at} is negative"
        raise JobDeadlineError(message)
    if timeout_minutes <= 0:
        message = f"timeout-minutes {timeout_minutes} is not positive"
        raise JobDeadlineError(message)
    return started_at + timeout_minutes * SECONDS_PER_MINUTE - DUMP_MARGIN_S


def workflow_jobs(workflow_text: str) -> dict[str, str]:
    """Map each job id under ``jobs:`` to the text indented beneath its key."""
    lines = workflow_text.splitlines()
    start = _jobs_line(lines)
    jobs: dict[str, str] = {}
    name: str | None = None
    body: list[str] = []
    for line in lines[start + 1 :]:
        matched = _JOB_KEY.match(line)
        if matched is not None:
            if name is not None:
                jobs[name] = "\n".join(body)
            name = matched.group(1)
            body = []
            continue
        if name is not None:
            body.append(line)
    if name is not None:
        jobs[name] = "\n".join(body)
    return jobs


def job_timeout_minutes(workflow_text: str, job: str) -> int:
    """The job-level ``timeout-minutes`` for ``job``.

    A step's own ``timeout-minutes`` is indented further and is not the job
    cancel, so it is ignored.
    """
    jobs = workflow_jobs(workflow_text)
    try:
        body = jobs[job]
    except KeyError as error:
        message = f"workflow has no job {job!r}"
        raise JobDeadlineError(message) from error
    for line in body.splitlines():
        matched = _TIMEOUT.match(line)
        if matched is not None:
            return int(matched.group(1))
    message = f"job {job!r} has no timeout-minutes"
    raise JobDeadlineError(message)


def arm(workflow_file: Path, job: str, started_at: int, env_file: Path) -> str:
    """Append the dump deadline for ``job`` to ``env_file`` and return that line."""
    minutes = job_timeout_minutes(workflow_file.read_text(encoding="utf-8"), job)
    line = f"{DUMP_AT_ENV}={dump_deadline(started_at, minutes)}\n"
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return line


def pytest_groups(checks: Mapping[str, object], scripts: Mapping[str, str]) -> set[str]:
    """Check-group names whose commands run pytest, following one script hop."""
    groups = checks.get("check_groups")
    if not isinstance(groups, list):
        return set()
    names: set[str] = set()
    for group in groups:
        if isinstance(group, dict) and _group_runs_pytest(group, scripts):
            name = group.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def workflow_gaps(
    workflow_name: str,
    workflow_text: str,
    groups: set[str],
    scripts: Mapping[str, str],
) -> list[str]:
    """Each pytest job in one workflow that does not pass its own deadline."""
    gaps: list[str] = []
    for job, body in workflow_jobs(workflow_text).items():
        if _earliest_pytest(body, groups, scripts) is None:
            continue
        gaps.extend(_job_gaps(workflow_name, job, body, groups, scripts))
    return gaps


def isolated_plugin_gaps(checks: Mapping[str, object], root: Path) -> list[str]:
    """Pytest projects that do not load the repository conftest, without the plugin."""
    gaps: list[str] = []
    for project in _isolated_pytest_roots(checks, root):
        conftest = project / "conftest.py"
        label = conftest.relative_to(root).as_posix()
        if not conftest.is_file():
            gaps.append(f"{label} is missing; it must load {PYTEST_PLUGIN}")
            continue
        text = conftest.read_text(encoding="utf-8")
        if PYTEST_PLUGIN not in text or DUMP_AT_ENV not in text:
            gaps.append(f"{label} must load {PYTEST_PLUGIN} when {DUMP_AT_ENV} is set")
    return gaps


def audit(root: Path) -> list[str]:
    """Deadline gaps for every workflow under ``root``."""
    checks_path = root / ".repoctl" / "checks.toml"
    checks = _load_toml(checks_path)
    workflows = _workflow_texts(root)
    scripts = _referenced_scripts(root, checks, workflows)
    groups = pytest_groups(checks, scripts)
    gaps: list[str] = []
    for name, text in sorted(workflows.items()):
        gaps.extend(workflow_gaps(name, text, groups, scripts))
    gaps.extend(isolated_plugin_gaps(checks, root))
    conftest = (root / "conftest.py").read_text(encoding="utf-8")
    if PYTEST_PLUGIN not in conftest:
        gaps.append(f"conftest.py must load {PYTEST_PLUGIN}")
    return gaps


def pytest_job_ids(root: Path) -> set[tuple[str, str]]:
    """``(workflow filename, job id)`` for every job that runs pytest."""
    checks = _load_toml(root / ".repoctl" / "checks.toml")
    workflows = _workflow_texts(root)
    scripts = _referenced_scripts(root, checks, workflows)
    groups = pytest_groups(checks, scripts)
    found: set[tuple[str, str]] = set()
    for name, text in workflows.items():
        for job, body in workflow_jobs(text).items():
            if _earliest_pytest(body, groups, scripts) is not None:
                found.add((name, job))
    return found


def workflow_filename(workflow_ref: str) -> str:
    """Filename of the workflow ``GITHUB_WORKFLOW_REF`` names.

    ``workflow_ref`` is ``<owner>/<repo>/.github/workflows/<file>@<ref>``.
    The display name in ``GITHUB_WORKFLOW`` is not a file and is not accepted
    here, including when a reusable workflow is running inside a caller whose
    name differs from the called file.
    """
    matched = _WORKFLOW_REF_FILE.search(workflow_ref)
    if matched is None or matched.group("name").startswith("."):
        message = f"GITHUB_WORKFLOW_REF {workflow_ref!r} does not name a workflow file"
        raise JobDeadlineError(message)
    return matched.group("name")


def arm_job(env: Mapping[str, str], workspace: Path, env_file: Path | None) -> str | None:
    """Append this job's dump deadline to ``env_file``.

    Return one warning, and leave ``env_file`` unchanged, when the deadline
    is missing or unparsable. The dump is a diagnostic and must not fail the
    job, so this does not raise for those inputs.
    """
    try:
        _write_deadline(env, workspace, env_file)
    except JobDeadlineError as error:
        return _skip_warning(str(error))
    except OSError as error:
        return _skip_warning(str(error))
    return None


def main() -> int:
    """Arm this job's dump deadline, or warn once and continue.

    A missing or unparsable deadline skips the dump. The status is always 0
    so the diagnostic cannot fail the job.
    """
    env_path = os.environ.get("GITHUB_ENV")
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    warning = arm_job(os.environ, workspace, Path(env_path) if env_path else None)
    if warning is not None:
        print(warning, file=sys.stderr)
    return 0


def _write_deadline(env: Mapping[str, str], workspace: Path, env_file: Path | None) -> None:
    if env_file is None:
        message = "missing GITHUB_ENV"
        raise JobDeadlineError(message)
    workflow = _workflow_path(workspace, _required(env, "GITHUB_WORKFLOW_REF"))
    arm(workflow, _required(env, "GITHUB_JOB"), _started_at(env), env_file)


def _workflow_path(workspace: Path, workflow_ref: str) -> Path:
    name = workflow_filename(workflow_ref)
    directory = (workspace / ".github" / "workflows").resolve()
    file = (directory / name).resolve()
    if file.parent != directory:
        message = f"GITHUB_WORKFLOW_REF {workflow_ref!r} does not name a workflow file"
        raise JobDeadlineError(message)
    if not file.is_file():
        message = f"workflow file {name} is not in the checkout"
        raise JobDeadlineError(message)
    return file


def _required(env: Mapping[str, str], key: str) -> str:
    try:
        return env[key]
    except KeyError as error:
        message = f"missing {key}"
        raise JobDeadlineError(message) from error


def _started_at(env: Mapping[str, str]) -> int:
    raw = _required(env, STARTED_AT_ENV)
    try:
        return int(raw)
    except ValueError as error:
        message = f"job start {raw!r} is not an integer"
        raise JobDeadlineError(message) from error


def _skip_warning(reason: str) -> str:
    flat = " ".join(reason.split())
    return f"pytest job deadline: {flat}; skipping the hang dump"


def _jobs_line(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if line == "jobs:" or line.startswith("jobs:"):
            return index
    message = "workflow has no jobs table"
    raise JobDeadlineError(message)


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _normalize(path: str) -> str:
    return path.removeprefix("./")


def _file_runs_pytest(text: str) -> bool:
    return _PYTEST.search(_code(text)) is not None


def _command_runs_pytest(command: object, scripts: Mapping[str, str]) -> bool:
    if not isinstance(command, list):
        return False
    for part in command:
        if part == "pytest":
            return True
        if not isinstance(part, str):
            continue
        path = _normalize(part)
        if path == _ARM_SCRIPT:
            continue
        script = scripts.get(path)
        if script is not None and _file_runs_pytest(script):
            return True
    return False


def _group_runs_pytest(group: Mapping[str, object], scripts: Mapping[str, str]) -> bool:
    commands = group.get("commands")
    if not isinstance(commands, list):
        return False
    return any(_command_runs_pytest(command, scripts) for command in commands)


def _earliest_pytest(body: str, groups: set[str], scripts: Mapping[str, str]) -> int | None:
    """Line number of the first run payload that starts pytest, or None."""
    code = _code(body)
    runs = _run_lines(code)
    found: list[int] = []
    for number, line in runs:
        found.extend(_pytest_lines(number, line, groups, scripts, code))
    return min(found) if found else None


def _pytest_lines(
    number: int,
    line: str,
    groups: set[str],
    scripts: Mapping[str, str],
    code: str,
) -> list[int]:
    found: list[int] = []
    if _PYTEST.search(line):
        found.append(number)
    found.extend(number for match in _GROUP.finditer(line) if match.group(1) in groups)
    if _MATRIX_GROUP.search(line) and any(group in code for group in groups):
        found.append(number)
    for match in _PATH.finditer(line):
        path = _normalize(match.group("path"))
        if path == _ARM_SCRIPT:
            continue
        script = scripts.get(path)
        if script is not None and _file_runs_pytest(script):
            found.append(number)
    return found


def _run_lines(code: str) -> list[tuple[int, str]]:
    """``(line number, text)`` for each ``run:`` payload, including block lines."""
    lines = code.splitlines()
    entries: list[tuple[int, str]] = []
    block_indent: int | None = None
    for number, line in enumerate(lines):
        if block_indent is not None:
            indent = len(line) - len(line.lstrip(" "))
            if line.strip() and indent <= block_indent:
                block_indent = None
            else:
                entries.append((number, line))
                continue
        if not line.lstrip().startswith("run:"):
            continue
        entries.append((number, line))
        rest = line.split("run:", 1)[1].strip()
        if rest[:1] in {"|", ">"}:
            block_indent = len(line) - len(line.lstrip(" "))
    return entries


def _line_of(code: str, snippet: str) -> int:
    for number, line in enumerate(code.splitlines()):
        if snippet in line:
            return number
    return -1


def _job_gaps(
    workflow_name: str,
    job: str,
    body: str,
    groups: set[str],
    scripts: Mapping[str, str],
) -> list[str]:
    where = f"{workflow_name} job {job}"
    try:
        minutes = job_timeout_minutes(f"jobs:\n  {job}:\n{body}\n", job)
    except JobDeadlineError as error:
        return [f"{where}: {error}"]
    gaps: list[str] = []
    if not 0 < DUMP_MARGIN_S < minutes * SECONDS_PER_MINUTE:
        gaps.append(
            f"{where}: dump margin {DUMP_MARGIN_S}s is not inside the {minutes} minute timeout"
        )
    code = _code(body)
    record = _line_of(code, RECORD_SNIPPET)
    checkout = _line_of(code, "actions/checkout")
    armed = _line_of(code, ARM_COMMAND)
    pytest_at = _earliest_pytest(body, groups, scripts)
    ordered = record >= 0 and checkout >= 0 and armed >= 0 and record < checkout < armed
    if not ordered:
        gaps.append(f"{where}: record {STARTED_AT_ENV} before checkout, then run {ARM_COMMAND}")
    elif pytest_at is None or armed >= pytest_at:
        gaps.append(f"{where}: {ARM_COMMAND} must run before pytest")
    return gaps


def _isolated_pytest_roots(checks: Mapping[str, object], root: Path) -> list[Path]:
    groups = checks.get("check_groups")
    if not isinstance(groups, list):
        return []
    found: list[Path] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        commands = group.get("commands")
        if not isinstance(commands, list):
            continue
        for command in commands:
            project = _project_root(command, root)
            if project is not None and project not in found:
                found.append(project)
    return found


def _project_root(command: object, root: Path) -> Path | None:
    if not isinstance(command, list) or "pytest" not in command or "--directory" not in command:
        return None
    directory = _flag(command, "--directory")
    if directory is None:
        return None
    project_flag = _flag(command, "--project")
    project = root / directory if project_flag is None else root / directory / project_flag
    if project.resolve() == root.resolve():
        return None
    config = project / "pyproject.toml"
    if not config.is_file() or "[tool.pytest" not in config.read_text(encoding="utf-8"):
        return None
    return project


def _flag(command: list[object], name: str) -> str | None:
    for index, part in enumerate(command[:-1]):
        value = command[index + 1]
        if part == name and isinstance(value, str):
            return value
    return None


def _load_toml(path: Path) -> Mapping[str, object]:
    loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _workflow_texts(root: Path) -> dict[str, str]:
    directory = root / ".github" / "workflows"
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.yml"))}


def _referenced_scripts(
    root: Path,
    checks: Mapping[str, object],
    workflows: Mapping[str, str],
) -> dict[str, str]:
    paths = _command_paths(checks)
    for text in workflows.values():
        paths.update(_normalize(match.group("path")) for match in _PATH.finditer(_code(text)))
    return _read_scripts(root, paths)


def _command_paths(checks: Mapping[str, object]) -> set[str]:
    groups = checks.get("check_groups")
    if not isinstance(groups, list):
        return set()
    paths: set[str] = set()
    for group in groups:
        paths.update(_paths_in_group(group))
    return paths


def _paths_in_group(group: object) -> set[str]:
    if not isinstance(group, dict):
        return set()
    commands = group.get("commands")
    if not isinstance(commands, list):
        return set()
    paths: set[str] = set()
    for command in commands:
        if isinstance(command, list):
            paths.update(_normalize(part) for part in command if isinstance(part, str))
    return paths


def _read_scripts(root: Path, paths: set[str]) -> dict[str, str]:
    scripts: dict[str, str] = {}
    for path in paths:
        if path == _ARM_SCRIPT or Path(path).suffix not in {".py", ".sh"}:
            continue
        file = root / path
        if file.is_file():
            scripts[path] = file.read_text(encoding="utf-8")
    return scripts


if __name__ == "__main__":
    raise SystemExit(main())
