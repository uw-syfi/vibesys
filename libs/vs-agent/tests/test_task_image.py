"""Public contract tests for task-image construction."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.images import (
    DockerCommandResult,
    TaskImageBuildError,
    build_task_image,
)
from vs_agent.api.testing import (
    DockerBuildResult,
    FakeDockerBuildRunner,
    docker_build_result,
    docker_result_without_output,
    docker_timed_out,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_IMAGE_ID = "sha256:" + "a" * 64


def _task(tmp_path: Path) -> Path:
    root = tmp_path / "task"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return root


class _FakeDockerImageStore:
    """The slice of Docker's local image state ``build_task_image`` depends on.

    ``build`` resolves to the same image ID for the same tag every time (as
    a real daemon's layer cache would for unchanged inputs), ``image
    inspect`` resolves a live tag to its ID, ``image ls`` lists a
    repository's live tags, and ``image rm`` drops one. Unscripted commands
    fail loudly rather than silently answering with a default.
    """

    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.builds: list[str] = []
        self.removed: list[str] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        on_stderr_line: Callable[[str], None] | None = None,
    ) -> DockerCommandResult:
        del cwd, timeout, on_stderr_line
        normalized = tuple(argv)
        if normalized[1] == "build":
            tag = normalized[normalized.index("--tag") + 1]
            self.builds.append(tag)
            self.tags.setdefault(tag, f"sha256:{hashlib.sha256(tag.encode()).hexdigest()}")
            return DockerCommandResult(args=normalized, returncode=0, stdout="", stderr="")
        if normalized[1] == "image" and normalized[2] == "inspect":
            image_id = self.tags.get(normalized[-1], "")
            return DockerCommandResult(
                args=normalized, returncode=0 if image_id else 1, stdout=image_id, stderr=""
            )
        if normalized[1] == "image" and normalized[2] == "ls":
            prefix = f"{normalized[-1]}:"
            suffixes = (tag.removeprefix(prefix) for tag in self.tags if tag.startswith(prefix))
            return DockerCommandResult(
                args=normalized, returncode=0, stdout="\n".join(suffixes), stderr=""
            )
        if normalized[1] == "image" and normalized[2] == "rm":
            tag = normalized[-1]
            self.tags.pop(tag, None)
            self.removed.append(tag)
            return DockerCommandResult(args=normalized, returncode=0, stdout="", stderr="")
        message = f"unscripted docker command in the Fake image store: {normalized}"
        raise AssertionError(message)


def test_build_reuses_the_existing_tag_and_image_for_an_unchanged_task(tmp_path: Path) -> None:
    """Regression for #2097: an unchanged task retags instead of growing the repository."""
    dockerfile = _task(tmp_path) / "Dockerfile"
    store = _FakeDockerImageStore()

    first_id = build_task_image(dockerfile, command_runner=store)
    second_id = build_task_image(dockerfile, command_runner=store)

    assert store.builds[0] == store.builds[1]
    assert first_id == second_id
    assert len(store.tags) == 1
    assert store.removed == []


def test_build_tags_a_changed_task_differently_and_prunes_the_old_tag(tmp_path: Path) -> None:
    """Regression for #2097: a changed task gets a new tag; the old one is pruned."""
    task_root = tmp_path / "task"
    task_root.mkdir()
    dockerfile = task_root / "Dockerfile"
    dockerfile.write_text("FROM scratch\nRUN echo one\n", encoding="utf-8")
    store = _FakeDockerImageStore()

    first_id = build_task_image(dockerfile, command_runner=store)

    dockerfile.write_text("FROM scratch\nRUN echo two\n", encoding="utf-8")
    second_id = build_task_image(dockerfile, command_runner=store)

    assert store.builds[0] != store.builds[1]
    assert first_id != second_id
    assert list(store.tags) == [store.builds[1]]
    assert store.removed == [store.builds[0]]


def test_build_tags_differ_by_base_image_argument(tmp_path: Path) -> None:
    """A task built against two different base images gets two distinct tags.

    The first tag is then superseded, so the launch that built the second
    prunes it: only the tag just built remains.
    """
    dockerfile = _task(tmp_path) / "Dockerfile"
    store = _FakeDockerImageStore()

    build_task_image(dockerfile, command_runner=store)
    build_task_image(dockerfile, base_image="python:3.12-bookworm", command_runner=store)

    assert store.builds[0] != store.builds[1]
    assert list(store.tags) == [store.builds[1]]


@given(content=st.text(max_size=200))
def test_build_tag_is_unchanged_when_the_context_is_rebuilt_unchanged(content: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        task_root = Path(directory)
        (task_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (task_root / "payload.txt").write_text(content, encoding="utf-8")
        store = _FakeDockerImageStore()

        build_task_image(task_root / "Dockerfile", command_runner=store)
        build_task_image(task_root / "Dockerfile", command_runner=store)

    assert store.builds[0] == store.builds[1]


@given(
    contents=st.tuples(st.text(max_size=200), st.text(max_size=200)).filter(
        lambda pair: pair[0] != pair[1]
    )
)
def test_build_tags_differ_whenever_the_context_changes(contents: tuple[str, str]) -> None:
    first_content, second_content = contents
    with tempfile.TemporaryDirectory() as directory:
        task_root = Path(directory)
        dockerfile = task_root / "Dockerfile"
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")
        payload = task_root / "payload.txt"
        store = _FakeDockerImageStore()

        payload.write_text(first_content, encoding="utf-8")
        build_task_image(dockerfile, command_runner=store)

        payload.write_text(second_content, encoding="utf-8")
        build_task_image(dockerfile, command_runner=store)

    assert store.builds[0] != store.builds[1]


def test_build_uses_task_context_and_returns_runnable_image_id(tmp_path: Path) -> None:
    task_root = _task(tmp_path)
    runner = FakeDockerBuildRunner()
    assert (
        build_task_image(task_root / "Dockerfile", command_runner=runner, timeout=42) == _IMAGE_ID
    )

    build_argv, cwd, timeout = runner.calls[0]
    tag = build_argv[4]
    assert tag.startswith("vibesys-task-build:")
    assert build_argv == (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        tag,
        "--file",
        str(task_root / "Dockerfile"),
        str(task_root),
    )
    assert cwd == task_root
    assert 0 < timeout <= 42
    inspect_argv, inspect_cwd, inspect_timeout = runner.calls[1]
    assert (inspect_argv, inspect_cwd) == (
        (
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            tag,
        ),
        task_root,
    )
    assert 0 < inspect_timeout <= 42


def test_build_passes_base_image_as_build_arg(tmp_path: Path) -> None:
    task_root = _task(tmp_path)
    runner = FakeDockerBuildRunner()
    build_task_image(
        task_root / "Dockerfile", base_image="python:3.12-bookworm", command_runner=runner
    )

    build_argv, _cwd, _timeout = runner.calls[0]
    tag = build_argv[4]
    assert tag.startswith("vibesys-task-build:")
    assert build_argv == (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        tag,
        "--file",
        str(task_root / "Dockerfile"),
        "--build-arg",
        "BASE_IMAGE=python:3.12-bookworm",
        str(task_root),
    )


@pytest.mark.parametrize(
    ("failure", "match"),
    [
        (FileNotFoundError(), "Docker was not found while building"),
        (
            docker_timed_out(("docker", "build"), 7),
            "budget of 7 seconds ran out while building",
        ),
    ],
)
def test_build_translates_process_failures(
    tmp_path: Path, failure: BaseException, match: str
) -> None:
    runner = FakeDockerBuildRunner(build_result=failure)

    with pytest.raises(TaskImageBuildError, match=match):
        build_task_image(_task(tmp_path) / "Dockerfile", command_runner=runner, timeout=7)

    assert len(runner.calls) == 1


def test_build_reports_bounded_truncated_docker_failure(tmp_path: Path) -> None:
    retained_tail = "x" * 2000 + "specific failure tail"
    runner = FakeDockerBuildRunner(
        build_result=DockerCommandResult(
            args=("docker",),
            returncode=17,
            stdout="ignored stdout",
            stderr=retained_tail,
            stderr_omitted_characters=2_000_000,
        ),
    )

    with pytest.raises(TaskImageBuildError, match=r"exit 17.*characters truncated") as caught:
        build_task_image(_task(tmp_path) / "Dockerfile", command_runner=runner)

    diagnostic = str(caught.value)
    assert diagnostic.endswith("specific failure tail")
    assert "ignored stdout" not in diagnostic
    assert len(diagnostic) < 1200


def test_build_uses_domain_fallback_when_legacy_runner_did_not_capture_output(
    tmp_path: Path,
) -> None:
    runner = FakeDockerBuildRunner(
        build_result=docker_result_without_output(("docker", "build"), returncode=17)
    )

    with pytest.raises(TaskImageBuildError, match=r"exit 17.*docker build failed"):
        build_task_image(_task(tmp_path) / "Dockerfile", command_runner=runner)


def test_build_rejects_truncated_stdout_even_when_tail_is_a_valid_image_id(
    tmp_path: Path,
) -> None:
    runner = FakeDockerBuildRunner(
        inspect_result=DockerCommandResult(
            args=("docker", "image", "inspect"),
            returncode=0,
            stdout=_IMAGE_ID,
            stderr="",
            stdout_omitted_characters=91,
        )
    )

    with pytest.raises(TaskImageBuildError, match="characters truncated"):
        build_task_image(_task(tmp_path) / "Dockerfile", command_runner=runner)


@pytest.mark.parametrize(
    ("image_id", "match"),
    [
        ("", "invalid runnable task image ID.*<empty>"),
        ("sha256:not-hex", "invalid runnable task image ID"),
        ("sha256:" + "a" * 63, "invalid runnable task image ID"),
        ("example:latest", "invalid runnable task image ID"),
    ],
)
def test_build_rejects_malformed_runnable_image_id(
    tmp_path: Path, image_id: str, match: str
) -> None:
    runner = FakeDockerBuildRunner(image_id=image_id)

    with pytest.raises(TaskImageBuildError, match=match):
        build_task_image(_task(tmp_path) / "Dockerfile", command_runner=runner)

    assert len(runner.calls) == 2


@pytest.mark.parametrize(
    ("failure", "match"),
    [
        (FileNotFoundError(), "Docker was not found while inspecting"),
        (
            docker_timed_out(("docker", "image", "inspect"), 7),
            "budget of 7 seconds ran out while inspecting",
        ),
        (
            docker_build_result(("docker",), 17, "ignored", "inspect failure"),
            r"Could not resolve runnable task image.*exit 17.*inspect failure",
        ),
    ],
)
def test_build_translates_inspect_failures(
    tmp_path: Path,
    failure: DockerBuildResult | BaseException,
    match: str,
) -> None:
    runner = FakeDockerBuildRunner(inspect_result=failure)

    with pytest.raises(TaskImageBuildError, match=match):
        build_task_image(_task(tmp_path) / "Dockerfile", command_runner=runner, timeout=7)

    assert len(runner.calls) == 2


def test_build_validates_dockerfile_before_running_docker(tmp_path: Path) -> None:
    runner = FakeDockerBuildRunner()

    task_root = tmp_path / "task"
    task_root.mkdir()
    with pytest.raises(TaskImageBuildError, match="Dockerfile must be a regular file"):
        build_task_image(task_root / "Dockerfile", command_runner=runner)
    (task_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    with pytest.raises(ValueError, match="timeout must be positive"):
        build_task_image(task_root / "Dockerfile", command_runner=runner, timeout=0)
    assert runner.calls == []


def test_build_rejects_dockerfile_symlink_without_widening_context(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    task_root.mkdir()
    outside = tmp_path / "outside.Dockerfile"
    outside.write_text("FROM scratch\n", encoding="utf-8")
    dockerfile = task_root / "Dockerfile"
    dockerfile.symlink_to(outside)
    runner = FakeDockerBuildRunner()

    with pytest.raises(TaskImageBuildError, match="must be a regular file"):
        build_task_image(dockerfile, command_runner=runner)

    assert runner.calls == []
