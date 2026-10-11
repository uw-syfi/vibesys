"""Building an image from a base plus Dev Container Features, with no Docker daemon."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import (
    FeatureImageBuildError,
    FinalImage,
    LocalFeature,
    build_feature_image,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sandbox.api import FeatureRef

_ID = "sha256:" + "d" * 64


def _local(root: Path, name: str, options: dict[str, str]) -> LocalFeature:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "devcontainer-feature.json").write_text(
        json.dumps(
            {
                "id": name,
                "options": {key: {"type": "string", "default": ""} for key in options},
                "containerEnv": {"TOOL": name},
            }
        )
    )
    (directory / "install.sh").write_text("#!/bin/sh\n")
    return LocalFeature(directory, options=options)


@dataclass(frozen=True)
class _Outcome:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _Seen:
    """What the build saw when it was "run": the real context is deleted afterwards."""

    context: Path
    dockerfile: str
    copied: frozenset[str]
    options_files: dict[str, str]


class _Runner:
    """Records Docker commands; reads the build context when `docker build` is "run"."""

    def __init__(
        self,
        *,
        fail_on: str | None = None,
        error: BaseException | None = None,
        image_ids: dict[str, str] | None = None,
        failure_stderr: str = "boom",
    ) -> None:
        self.failure_stderr = failure_stderr
        self.image_ids = image_ids or {}
        self.calls: list[tuple[str, ...]] = []
        self.seen: list[_Seen] = []
        self.fail_on = fail_on
        self.error = error

    def run(self, argv: Sequence[str], *, cwd: Path, timeout: float) -> _Outcome:
        del cwd, timeout
        call = tuple(argv)
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        if call[1] == "build":
            context = Path(call[-1])
            features = context / "features"
            self.seen.append(
                _Seen(
                    context,
                    Path(call[call.index("--file") + 1]).read_text(),
                    frozenset(p.name for p in features.iterdir()),
                    {
                        p.name: (p / "devcontainer-features.env").read_text()
                        for p in features.iterdir()
                    },
                )
            )
        if self.fail_on == call[1]:
            return _Outcome(3, "", self.failure_stderr)
        stdout = self.image_ids.get(call[-1], _ID) if call[1:3] == ("image", "inspect") else ""
        return _Outcome(0, stdout, "")


def _build(runner: _Runner, features: Sequence[FeatureRef]) -> str:
    return build_feature_image(
        "base:1",
        features,
        FinalImage(user="agent", workdir="/workspace", image_repository="demo-build"),
        command_runner=runner,
        timeout=7,
    ).image_id


def test_build_is_one_docker_build_then_an_inspect(tmp_path: Path) -> None:
    runner = _Runner()
    features = [_local(tmp_path, "tool-a", {"v": "1"}), _local(tmp_path, "tool-b", {})]

    assert _build(runner, features) == _ID

    assert [call[1:3] if call[1] == "image" else call[1:2] for call in runner.calls] == [
        ("image", "inspect"),
        ("build",),
        ("image", "inspect"),
    ]
    build = runner.calls[1]
    (seen,) = runner.seen
    assert build[build.index("--build-arg") + 1] == "BASE_IMAGE=base:1"
    assert build[build.index("--tag") + 1].startswith("demo-build:")
    assert seen.copied == {"tool-a", "tool-b"}
    assert seen.options_files["tool-a"].splitlines()[-1] == "V=1"
    assert seen.dockerfile.index("tool-a") < seen.dockerfile.index("tool-b")
    assert seen.dockerfile.splitlines()[-2:] == ["WORKDIR /workspace", "USER agent"]
    assert not seen.context.exists()


def _tags(runner: _Runner) -> list[str]:
    return [c[c.index("--tag") + 1] for c in runner.calls if c[1] == "build"]


def test_rebuilding_the_same_inputs_reuses_one_tag(tmp_path: Path) -> None:
    runner = _Runner()
    features = [_local(tmp_path, "tool-a", {"v": "1"}), _local(tmp_path, "tool-b", {})]

    _build(runner, features)
    _build(runner, features)
    elsewhere = _Runner()
    _build(
        elsewhere,
        [
            _local(tmp_path / "other", "tool-a", {"v": "1"}),
            _local(tmp_path / "other", "tool-b", {}),
        ],
    )

    first, second = _tags(runner)
    assert first == second
    assert _tags(elsewhere) == [first]


@pytest.mark.parametrize(
    "variant",
    ["option", "order", "feature", "user", "workdir", "base", "content", "repository"],
)
def test_different_inputs_get_different_tags(tmp_path: Path, variant: str) -> None:
    def tag_for(root: Path, **changes: object) -> str:
        runner = _Runner(image_ids={"base:1": "sha256:" + "a" * 64, "base:2": "sha256:" + "b" * 64})
        a = _local(root, "tool-a", {"v": str(changes.get("option", "1"))})
        b = _local(root, "tool-b", {})
        if changes.get("content"):
            (a.directory / "install.sh").write_text("#!/bin/sh\necho changed\n")
        features = {
            "order": [b, a],
            "feature": [a],
        }.get(str(changes.get("shape")), [a, b])
        build_feature_image(
            str(changes.get("base", "base:1")),
            features,
            FinalImage(
                user=str(changes.get("user", "agent")),
                workdir=str(changes.get("workdir", "/workspace")),
                image_repository=str(changes.get("repository", "demo-build")),
            ),
            command_runner=runner,
            timeout=7,
        )
        return _tags(runner)[0]

    changes: dict[str, dict[str, object]] = {
        "option": {"option": "2"},
        "order": {"shape": "order"},
        "feature": {"shape": "feature"},
        "user": {"user": "root"},
        "workdir": {"workdir": "/other"},
        "base": {"base": "base:2"},
        "content": {"content": True},
        "repository": {"repository": "other-build"},
    }
    assert tag_for(tmp_path / "x") != tag_for(tmp_path / "y", **changes[variant])


def test_a_base_with_a_new_name_but_the_same_id_keeps_the_tag(tmp_path: Path) -> None:
    runner = _Runner()
    features = [_local(tmp_path, "tool-a", {})]
    final = FinalImage(user="agent", workdir="/workspace", image_repository="demo-build")

    for base in ("task-build:1", "task-build:2"):
        build_feature_image(base, features, final, command_runner=runner, timeout=7)

    first, second = _tags(runner)
    assert first == second


def test_a_build_never_mounts_the_docker_socket_or_runs_a_container(tmp_path: Path) -> None:
    """Hardened hosts refuse a bind mount of the socket (exit 125); no argv may ask for one."""
    runner = _Runner()

    _build(runner, [_local(tmp_path, "tool-a", {})])

    for call in runner.calls:
        assert call[1] != "run"
        assert not any("docker.sock" in token for token in call)
        assert not any(token in {"--volume", "-v", "--mount", "--privileged"} for token in call)


def test_an_option_the_feature_does_not_declare_fails_before_any_docker_command(
    tmp_path: Path,
) -> None:
    runner = _Runner()
    feature = _local(tmp_path, "tool-a", {"v": "1"})
    wrong = LocalFeature(feature.directory, options={"typo": "1"})

    with pytest.raises(FeatureImageBuildError, match="declares no option"):
        _build(runner, [wrong])

    assert runner.calls == []


@pytest.mark.parametrize("field", ["mounts", "privileged", "entrypoint"])
def test_a_feature_with_an_unsupported_field_fails_before_any_docker_command(
    tmp_path: Path, field: str
) -> None:
    runner = _Runner()
    feature = _local(tmp_path, "tool-a", {})
    metadata = feature.directory / "devcontainer-feature.json"
    metadata.write_text(json.dumps({**json.loads(metadata.read_text()), field: True}))

    with pytest.raises(FeatureImageBuildError, match=f"unsupported metadata field.*{field}"):
        _build(runner, [feature])

    assert runner.calls == []


def test_a_folder_without_feature_files_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FeatureImageBuildError, match="not a Feature"):
        _build(_Runner(), [LocalFeature(tmp_path)])


def test_a_failed_build_names_the_step_and_cleans_the_context(tmp_path: Path) -> None:
    runner = _Runner(fail_on="build")

    with pytest.raises(
        FeatureImageBuildError, match=r"feature image build failed \(exit 3\): boom"
    ):
        _build(runner, [_local(tmp_path, "tool-a", {})])

    assert not runner.seen[0].context.exists()


def test_missing_docker_and_timeouts_are_build_errors(tmp_path: Path) -> None:
    feature = _local(tmp_path, "tool-a", {})

    with pytest.raises(FeatureImageBuildError, match="not found"):
        _build(_Runner(error=FileNotFoundError("docker")), [feature])
    with pytest.raises(FeatureImageBuildError, match="timed out after 7 seconds"):
        _build(_Runner(error=TimeoutError()), [feature])


def test_a_timeout_keeps_what_the_stopped_command_printed(tmp_path: Path) -> None:
    feature = _local(tmp_path, "tool-a", {})

    with pytest.raises(FeatureImageBuildError, match="ETIMEDOUT fetching the tarball"):
        _build(_Runner(error=TimeoutError("ETIMEDOUT fetching the tarball")), [feature])


@given(printed=st.text(min_size=1, max_size=5000).filter(lambda text: text.strip() != ""))
def test_a_failed_build_reports_the_end_of_its_output(printed: str) -> None:
    """A build prints the failing step last, so the tail is what the error keeps."""
    with tempfile.TemporaryDirectory() as directory:
        feature = _local(Path(directory), "tool-a", {})
        runner = _Runner(fail_on="build", failure_stderr="x" * 3000 + printed)

        with pytest.raises(FeatureImageBuildError) as caught:
            _build(runner, [feature])

    assert str(caught.value).endswith(("x" * 3000 + printed).strip()[-100:])


def test_a_nonpositive_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        build_feature_image(
            "base:1",
            [],
            FinalImage(user="agent", workdir="/workspace", image_repository="demo-build"),
            command_runner=_Runner(),
            timeout=0,
        )
