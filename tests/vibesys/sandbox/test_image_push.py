"""Tests for pushing and verifying agent images in a registry.

:func:`push_agent_image`, :func:`agent_image_is_pushed`, and
:func:`ensure_pushed` are the remote-backend half of ``vibesys.sandbox.images``
(see ``agent_image`` itself, covered by ``test_images.py``). A local
``--docker`` run never calls any of these; only Modal and SkyPilot do, once
their run environment resolves the agent image it needs to run from.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vibesys.sandbox.images import (
    DEFAULT_AGENT_IMAGE_REGISTRY,
    ImagePushError,
    agent_image_is_pushed,
    ensure_pushed,
    push_agent_image,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_IMAGE_ID = "sha256:" + "c" * 64
_SHORT_ID = "c" * 12
_TAG = f"{DEFAULT_AGENT_IMAGE_REGISTRY}:{_SHORT_ID}"
_DIGEST = f"{DEFAULT_AGENT_IMAGE_REGISTRY}@sha256:" + "d" * 64


class _FakeRegistryRunner:
    """Programmable fake for the docker tag/push/inspect/manifest sequence."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        tag_returncode: int = 0,
        push_returncode: int = 0,
        push_stderr: str = "",
        inspect_stdout: str = _DIGEST,
        inspect_returncode: int = 0,
        manifest_returncode: int = 0,
        unverifiable_references: frozenset[str] = frozenset(),
        cached_repo_digests: str | None = None,
        raise_on: dict[str, Exception] | None = None,
    ) -> None:
        self.tag_returncode = tag_returncode
        self.push_returncode = push_returncode
        self.push_stderr = push_stderr
        self.inspect_stdout = inspect_stdout
        self.inspect_returncode = inspect_returncode
        self.manifest_returncode = manifest_returncode
        # References `docker manifest inspect` should report as absent even
        # though `manifest_returncode` is otherwise 0: lets a test make one
        # specific (usually stale) digest fail verification while a freshly
        # pushed one still succeeds.
        self.unverifiable_references = unverifiable_references
        self.cached_repo_digests = cached_repo_digests
        self.raise_on = raise_on or {}
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,  # noqa: ARG002
        timeout: float,  # noqa: ARG002
    ) -> subprocess.CompletedProcess[str]:
        normalized = tuple(argv)
        self.calls.append(normalized)
        for marker, exc in self.raise_on.items():
            if marker in normalized:
                raise exc
        if normalized[1] == "tag":
            return subprocess.CompletedProcess(normalized, self.tag_returncode, "", "")
        if normalized[1] == "push":
            return subprocess.CompletedProcess(
                normalized, self.push_returncode, "", self.push_stderr
            )
        if normalized[1] == "manifest":
            reference = normalized[-1]
            returncode = (
                1 if reference in self.unverifiable_references else self.manifest_returncode
            )
            return subprocess.CompletedProcess(normalized, returncode, "", "")
        if normalized[1] == "image" and normalized[2] == "inspect":
            # The cached-digest fast path: `docker image inspect --format
            # {{json .RepoDigests}} <image_id>`.
            stdout = self.cached_repo_digests if self.cached_repo_digests is not None else "[]"
            return subprocess.CompletedProcess(normalized, 0, stdout, "")
        if normalized[1] == "inspect":
            # The post-push digest resolution: `docker inspect --format
            # {{index .RepoDigests 0}} <tag>`.
            return subprocess.CompletedProcess(
                normalized, self.inspect_returncode, self.inspect_stdout, ""
            )
        raise AssertionError(f"unexpected command: {normalized}")  # noqa: TRY003


class TestPushAgentImage:
    def test_tags_pushes_and_resolves_digest(self) -> None:
        runner = _FakeRegistryRunner()

        reference = push_agent_image(_IMAGE_ID, command_runner=runner)

        assert reference == _DIGEST
        assert runner.calls[0] == ("docker", "tag", _IMAGE_ID, _TAG)
        assert runner.calls[1] == ("docker", "push", _TAG)
        assert runner.calls[2] == (
            "docker",
            "inspect",
            "--format",
            "{{index .RepoDigests 0}}",
            _TAG,
        )

    def test_uses_content_derived_tag_not_a_moving_tag(self) -> None:
        runner = _FakeRegistryRunner()
        push_agent_image(_IMAGE_ID, command_runner=runner)
        assert runner.calls[0][3] == f"{DEFAULT_AGENT_IMAGE_REGISTRY}:{_SHORT_ID}"

    def test_custom_repository(self) -> None:
        runner = _FakeRegistryRunner()
        repository = "ghcr.io/example/other-agent"
        digest = f"{repository}@sha256:" + "e" * 64
        runner.inspect_stdout = digest

        reference = push_agent_image(_IMAGE_ID, repository=repository, command_runner=runner)

        assert reference == digest
        assert runner.calls[0][3] == f"{repository}:{_SHORT_ID}"

    def test_rejects_mutable_tag_as_image_id(self) -> None:
        with pytest.raises(ValueError, match="immutable image ID"):
            push_agent_image("latest", command_runner=_FakeRegistryRunner())

    def test_rejects_nonpositive_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout must be positive"):
            push_agent_image(_IMAGE_ID, command_runner=_FakeRegistryRunner(), timeout=0)

    def test_tag_failure_raises_with_detail(self) -> None:
        runner = _FakeRegistryRunner(tag_returncode=1)
        with pytest.raises(ImagePushError, match="Could not tag"):
            push_agent_image(_IMAGE_ID, command_runner=runner)

    def test_push_failure_names_docker_login_prerequisite(self) -> None:
        runner = _FakeRegistryRunner(push_returncode=1, push_stderr="denied: permission_denied")
        with pytest.raises(ImagePushError, match=r"docker login ghcr\.io") as excinfo:
            push_agent_image(_IMAGE_ID, command_runner=runner)
        assert "permission_denied" in str(excinfo.value)

    def test_push_failure_never_logs_credentials(self) -> None:
        runner = _FakeRegistryRunner(
            push_returncode=1, push_stderr="Authorization: Bearer super-secret-token"
        )
        with pytest.raises(ImagePushError):
            push_agent_image(_IMAGE_ID, command_runner=runner)
        # The docker CLI's own diagnostic text may be echoed back (Docker
        # itself is trusted not to echo the token used for login), but this
        # module never constructs or forwards a credential value of its own.
        assert "super-secret-token" not in "".join(
            arg for call in runner.calls for arg in call if isinstance(arg, str)
        )

    def test_missing_repo_digest_after_push_raises(self) -> None:
        runner = _FakeRegistryRunner(inspect_stdout="")
        with pytest.raises(ImagePushError, match="no repo digest"):
            push_agent_image(_IMAGE_ID, command_runner=runner)

    def test_unexpected_repo_digest_raises(self) -> None:
        runner = _FakeRegistryRunner(inspect_stdout="some-other-repo@sha256:" + "f" * 64)
        with pytest.raises(ImagePushError, match="unexpected repo digest"):
            push_agent_image(_IMAGE_ID, command_runner=runner)

    def test_missing_docker_binary_raises(self) -> None:
        runner = _FakeRegistryRunner(raise_on={"tag": FileNotFoundError()})
        with pytest.raises(ImagePushError, match="Docker was not found"):
            push_agent_image(_IMAGE_ID, command_runner=runner)

    def test_timeout_raises(self) -> None:
        runner = _FakeRegistryRunner(raise_on={"tag": subprocess.TimeoutExpired("docker", 5)})
        with pytest.raises(ImagePushError, match="timed out"):
            push_agent_image(_IMAGE_ID, command_runner=runner)


class TestAgentImageIsPushed:
    def test_true_when_manifest_inspect_succeeds(self) -> None:
        runner = _FakeRegistryRunner(manifest_returncode=0)
        assert agent_image_is_pushed(_DIGEST, command_runner=runner) is True
        assert runner.calls == [("docker", "manifest", "inspect", _DIGEST)]

    def test_false_when_manifest_inspect_fails(self) -> None:
        runner = _FakeRegistryRunner(manifest_returncode=1)
        assert agent_image_is_pushed(_DIGEST, command_runner=runner) is False

    def test_false_when_docker_missing(self) -> None:
        runner = _FakeRegistryRunner(raise_on={"manifest": FileNotFoundError()})
        assert agent_image_is_pushed(_DIGEST, command_runner=runner) is False

    def test_false_on_timeout(self) -> None:
        runner = _FakeRegistryRunner(raise_on={"manifest": subprocess.TimeoutExpired("docker", 5)})
        assert agent_image_is_pushed(_DIGEST, command_runner=runner) is False


class TestEnsurePushed:
    def test_pushes_when_no_cached_digest(self) -> None:
        runner = _FakeRegistryRunner()

        reference = ensure_pushed(_IMAGE_ID, command_runner=runner)

        assert reference == _DIGEST
        assert ("docker", "tag", _IMAGE_ID, _TAG) in runner.calls
        assert ("docker", "push", _TAG) in runner.calls

    def test_skips_push_when_cached_digest_still_verifies(self) -> None:
        runner = _FakeRegistryRunner(cached_repo_digests=f'["{_DIGEST}"]', manifest_returncode=0)

        reference = ensure_pushed(_IMAGE_ID, command_runner=runner)

        assert reference == _DIGEST
        assert not any(call[1] == "push" for call in runner.calls)
        assert not any(call[1] == "tag" for call in runner.calls)

    def test_pushes_when_cached_digest_no_longer_verifies(self) -> None:
        stale_digest = f"{DEFAULT_AGENT_IMAGE_REGISTRY}@sha256:" + "1" * 64
        runner = _FakeRegistryRunner(
            cached_repo_digests=f'["{stale_digest}"]',
            unverifiable_references=frozenset({stale_digest}),
        )

        reference = ensure_pushed(_IMAGE_ID, command_runner=runner)

        assert reference == _DIGEST
        assert any(call[1] == "push" for call in runner.calls)

    def test_ignores_cached_digest_for_a_different_repository(self) -> None:
        runner = _FakeRegistryRunner(
            cached_repo_digests='["ghcr.io/someone-else/agent@sha256:' + "a" * 64 + '"]'
        )

        ensure_pushed(_IMAGE_ID, command_runner=runner)

        assert any(call[1] == "push" for call in runner.calls)

    def test_raises_a_clear_error_naming_the_digest_when_unverifiable_after_push(self) -> None:
        runner = _FakeRegistryRunner(manifest_returncode=1)
        with pytest.raises(ImagePushError, match=_DIGEST):
            ensure_pushed(_IMAGE_ID, command_runner=runner)

    def test_propagates_push_failure(self) -> None:
        runner = _FakeRegistryRunner(push_returncode=1)
        with pytest.raises(ImagePushError, match="Could not push"):
            ensure_pushed(_IMAGE_ID, command_runner=runner)
