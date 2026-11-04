"""Registry cache identity depends on install inputs, never the GitHub run/ref."""

from __future__ import annotations

import hashlib

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from scripts.ci_image_cache import image_cache_reference, main


@given(first=st.text(min_size=1).filter(str.strip), second=st.text(min_size=1).filter(str.strip))
@example(first="feature-rocm-base-a-input-a", second="feature-rocm-base-a-input-b")
def test_snapshot_identity_changes_exactly_when_inputs_change(first: str, second: str) -> None:
    a = image_cache_reference("owner/repo", first)
    b = image_cache_reference("owner/repo", second)
    assert (a == b) == (first == second)
    assert a == f"ghcr.io/owner/repo/ci-image-cache:{hashlib.sha256(first.encode()).hexdigest()}"
    assert len(a.rsplit(":", 1)[1]) <= 128


@pytest.mark.parametrize("repository", ["owner/repo", "Owner/Repo", "owner-x/repo.name"])
def test_repository_case_does_not_split_the_shared_cache(repository: str) -> None:
    assert image_cache_reference(repository, "inputs") == image_cache_reference(
        repository.lower(), "inputs"
    )


@pytest.mark.parametrize("repository", ["", "owner", "owner/repo/extra", "owner/repo\n", "../repo"])
def test_invalid_repositories_fail_before_registry_access(repository: str) -> None:
    with pytest.raises(ValueError, match="invalid GitHub repository"):
        image_cache_reference(repository, "inputs")


@pytest.mark.parametrize("key", ["", " ", "\n"])
def test_empty_cache_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="key must not be empty"):
        image_cache_reference("owner/repo", key)


def test_workflow_cli_prints_the_shared_registry_reference(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["owner/repo", "install-inputs"])
    assert capsys.readouterr().out == image_cache_reference("owner/repo", "install-inputs") + "\n"


@pytest.mark.parametrize("argv", [["owner/repo"], ["owner/repo", "inputs", "--unknown-key"]])
def test_workflow_cli_rejects_missing_inputs_and_unknown_options(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(argv)
    assert error.value.code == 2
