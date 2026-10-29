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
    is_task_image_reference,
    local_image_id,
    task_image_inputs,
    task_image_reference,
)
from vs_agent.api.testing import (
    DockerBuildResult,
    FakeDockerBuildRunner,
    docker_build_result,
    docker_result_without_output,
    docker_timed_out,
)
from vs_sim.api import DISCARD
from vs_sim.api.testing import ManualClock

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sim.api import OutputSink

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
        output: OutputSink,
    ) -> DockerCommandResult:
        del cwd, timeout, output
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

    first_id = build_task_image(
        dockerfile, command_runner=store, clock=ManualClock(), output=DISCARD
    )
    second_id = build_task_image(
        dockerfile, command_runner=store, clock=ManualClock(), output=DISCARD
    )

    assert len(store.builds) == 1
    assert first_id == second_id
    assert len(store.tags) == 1
    assert store.removed == []


@given(first=st.sampled_from(["a", "b", "c"]), second=st.sampled_from(["a", "b", "c"]))
def test_a_task_is_built_again_exactly_when_its_inputs_changed(first: str, second: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        task_root = Path(directory)
        dockerfile = task_root / "Dockerfile"
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")
        store = _FakeDockerImageStore()
        for payload in (first, second):
            (task_root / "payload.txt").write_text(payload, encoding="utf-8")
            build_task_image(dockerfile, command_runner=store, clock=ManualClock(), output=DISCARD)

    assert len(store.builds) == (1 if first == second else 2)


def test_build_tags_a_changed_task_differently_and_prunes_the_old_tag(tmp_path: Path) -> None:
    """Regression for #2097: a changed task gets a new tag; the old one is pruned."""
    task_root = tmp_path / "task"
    task_root.mkdir()
    dockerfile = task_root / "Dockerfile"
    dockerfile.write_text("FROM scratch\nRUN echo one\n", encoding="utf-8")
    store = _FakeDockerImageStore()

    first_id = build_task_image(
        dockerfile, command_runner=store, clock=ManualClock(), output=DISCARD
    )

    dockerfile.write_text("FROM scratch\nRUN echo two\n", encoding="utf-8")
    second_id = build_task_image(
        dockerfile, command_runner=store, clock=ManualClock(), output=DISCARD
    )

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

    build_task_image(dockerfile, command_runner=store, clock=ManualClock(), output=DISCARD)
    build_task_image(
        dockerfile,
        base_image="python:3.12-bookworm",
        command_runner=store,
        clock=ManualClock(),
        output=DISCARD,
    )

    assert store.builds[0] != store.builds[1]
    assert list(store.tags) == [store.builds[1]]


@given(content=st.text(max_size=200))
def test_build_tag_is_unchanged_when_the_context_is_rebuilt_unchanged(content: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        task_root = Path(directory)
        (task_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (task_root / "payload.txt").write_text(content, encoding="utf-8")
        store = _FakeDockerImageStore()

        build_task_image(
            task_root / "Dockerfile", command_runner=store, clock=ManualClock(), output=DISCARD
        )
        build_task_image(
            task_root / "Dockerfile", command_runner=store, clock=ManualClock(), output=DISCARD
        )

    assert len(store.builds) == 1


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
        build_task_image(dockerfile, command_runner=store, clock=ManualClock(), output=DISCARD)

        payload.write_text(second_content, encoding="utf-8")
        build_task_image(dockerfile, command_runner=store, clock=ManualClock(), output=DISCARD)

    assert store.builds[0] != store.builds[1]


def test_build_uses_task_context_and_returns_runnable_image_id(tmp_path: Path) -> None:
    task_root = _task(tmp_path)
    runner = FakeDockerBuildRunner()
    assert (
        build_task_image(
            task_root / "Dockerfile",
            command_runner=runner,
            timeout=42,
            clock=ManualClock(),
            output=DISCARD,
        )
        == _IMAGE_ID
    )

    build_argv, cwd, timeout = runner.calls[1]
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
    inspect_argv, inspect_cwd, inspect_timeout = runner.calls[2]
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
        task_root / "Dockerfile",
        base_image="python:3.12-bookworm",
        command_runner=runner,
        clock=ManualClock(),
        output=DISCARD,
    )

    build_argv, _cwd, _timeout = runner.calls[1]
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
        build_task_image(
            _task(tmp_path) / "Dockerfile",
            command_runner=runner,
            timeout=7,
            clock=ManualClock(),
            output=DISCARD,
        )

    assert len(runner.calls) == 2


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
        build_task_image(
            _task(tmp_path) / "Dockerfile",
            command_runner=runner,
            clock=ManualClock(),
            output=DISCARD,
        )

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
        build_task_image(
            _task(tmp_path) / "Dockerfile",
            command_runner=runner,
            clock=ManualClock(),
            output=DISCARD,
        )


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
        build_task_image(
            _task(tmp_path) / "Dockerfile",
            command_runner=runner,
            clock=ManualClock(),
            output=DISCARD,
        )


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
        build_task_image(
            _task(tmp_path) / "Dockerfile",
            command_runner=runner,
            clock=ManualClock(),
            output=DISCARD,
        )

    assert len(runner.calls) == 3


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
        build_task_image(
            _task(tmp_path) / "Dockerfile",
            command_runner=runner,
            timeout=7,
            clock=ManualClock(),
            output=DISCARD,
        )

    assert len(runner.calls) == 3


def test_build_validates_dockerfile_before_running_docker(tmp_path: Path) -> None:
    runner = FakeDockerBuildRunner()

    task_root = tmp_path / "task"
    task_root.mkdir()
    with pytest.raises(TaskImageBuildError, match="Dockerfile must be a regular file"):
        build_task_image(
            task_root / "Dockerfile", command_runner=runner, clock=ManualClock(), output=DISCARD
        )
    (task_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    with pytest.raises(ValueError, match="timeout must be positive"):
        build_task_image(
            task_root / "Dockerfile",
            command_runner=runner,
            timeout=0,
            clock=ManualClock(),
            output=DISCARD,
        )
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
        build_task_image(dockerfile, command_runner=runner, clock=ManualClock(), output=DISCARD)

    assert runner.calls == []


def test_a_build_never_prunes_another_tasks_tag(tmp_path: Path) -> None:
    """Regression: a build pruned every task's tag on the daemon, so concurrent runs
    (other tasks, other checkouts, other users of a shared host) lost their images."""
    store = _FakeDockerImageStore()
    first = tmp_path / "one" / "Dockerfile"
    second = tmp_path / "two" / "Dockerfile"
    for dockerfile in (first, second):
        dockerfile.parent.mkdir()
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    build_task_image(first, command_runner=store, clock=ManualClock(), output=DISCARD)
    build_task_image(second, command_runner=store, clock=ManualClock(), output=DISCARD)
    second.write_text("FROM scratch\nRUN echo changed\n", encoding="utf-8")
    build_task_image(second, command_runner=store, clock=ManualClock(), output=DISCARD)

    assert store.builds[0] in store.tags
    assert store.removed == [store.builds[1]]


@given(base=st.one_of(st.none(), st.text(min_size=1, max_size=30)))
def test_the_reference_is_the_tag_the_build_produces(base: str | None) -> None:
    with tempfile.TemporaryDirectory() as directory:
        dockerfile = Path(directory) / "Dockerfile"
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")
        store = _FakeDockerImageStore()
        reference = task_image_reference(dockerfile, base_image=base)

        assert (
            local_image_id(reference, command_runner=store, output=DISCARD, clock=ManualClock())
            is None
        )
        build_task_image(
            dockerfile, base_image=base, command_runner=store, clock=ManualClock(), output=DISCARD
        )

        assert store.builds == [reference]
        assert is_task_image_reference(reference)
        assert (
            local_image_id(reference, command_runner=store, output=DISCARD, clock=ManualClock())
            == store.tags[reference]
        )


@given(content=st.text(max_size=200), base=st.one_of(st.none(), st.just("python:3")))
def test_identical_inputs_at_two_locations_have_the_same_inputs_digest(
    content: str, base: str | None
) -> None:
    """Regression: a copied project (``--runs-dir``) counted as changed task inputs."""
    with tempfile.TemporaryDirectory() as directory:
        references = []
        for location in ("a", "b"):
            task = Path(directory) / location / "t"
            task.mkdir(parents=True)
            (task / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (task / "payload.txt").write_text(content, encoding="utf-8")
            references.append(task_image_reference(task / "Dockerfile", base_image=base))

    first, second = references
    assert first != second  # the owner, which scopes pruning, differs
    assert task_image_inputs(first) == task_image_inputs(second) is not None


def test_a_non_task_image_has_no_inputs_digest() -> None:
    assert task_image_inputs(_IMAGE_ID) is None
    assert task_image_inputs("python:3.12-bookworm") is None


_IGNORE_CASES = [
    # (.dockerignore, edited file, whether the edit is a build input)
    ("README.md\n", "README.md", False),
    ("*.md\n", "docs/notes.md", True),  # ``*`` stays within one segment
    ("**/*.md\n", "docs/notes.md", False),
    ("docs\n", "docs/notes.md", False),  # a directory pattern covers its contents
    ("docs\n!docs/keep.md\n", "docs/keep.md", True),
    ("# comment\n/README.md\n", "README.md", False),
    ("*\n!Dockerfile\n", "README.md", False),
    ("*\n", "Dockerfile", True),  # the Dockerfile is always an input
    ("*\n", ".dockerignore", True),
    ("bench?.py\n", "bench1.py", False),
    ("bench[0-9].py\n", "benchx.py", True),
    ("", "README.md", True),
]


@pytest.mark.parametrize(("dockerignore", "edited", "is_input"), _IGNORE_CASES)
def test_the_inputs_digest_honors_dockerignore(
    dockerignore: str, edited: str, *, is_input: bool, tmp_path: Path
) -> None:
    """Regression: editing a file the build never sees (a README) changed the task's inputs."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    (tmp_path / ".dockerignore").write_text(dockerignore, encoding="utf-8")
    target = tmp_path / edited
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("before\n", encoding="utf-8")
    before = task_image_reference(dockerfile, base_image=None)

    with target.open("a", encoding="utf-8") as file:
        file.write("# edited\n")

    assert (task_image_reference(dockerfile, base_image=None) != before) is is_input
