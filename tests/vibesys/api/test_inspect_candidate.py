"""Candidate inspection is a typed read, with the helper's minimum version."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from server.host_compatibility_generated import MIN_CHECKOUT_VERSION
from vibesys.api.host_operations.api import execute_inspect_candidate
from vs_proto.api import (
    API_SCHEMA_HASH,
    FailureCategory,
    Oneof,
    OperationFailure,
    ReadRequest,
    ReadResponse,
)
from vs_proto.api import host_payloads as pb

if TYPE_CHECKING:
    from pathlib import Path

_COMMIT = "a" * 40


def _request(path: str) -> bytes:
    return ReadRequest(
        checkout=path or "~",
        method=Oneof(
            "inspect_candidate",
            pb.InspectCandidateRequest(request_id="req-1", path=path, selection_revision=7),
        ),
    ).to_binary()


def _result(path: str) -> object:
    response = ReadResponse.from_binary(execute_inspect_candidate(_request(path)))
    assert response.result is not None
    return response.result.value


def _checkout(root: Path, version: str) -> None:
    project = root / "pyproject.toml"
    project.write_text(
        f'[project]\nname = "vibesys"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    helper = root / "src" / "entrypoints" / "host_helper.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("# helper\n", encoding="utf-8")
    (root / ".vibesys" / "tasks").mkdir(parents=True)
    git = root / ".git"
    git.mkdir()
    (git / "HEAD").write_text(_COMMIT + "\n", encoding="utf-8")


def test_minimum_version_matches_the_helper_and_a_current_checkout_is_found(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    _checkout(root, MIN_CHECKOUT_VERSION)
    reply = _result(str(root))
    assert isinstance(reply, pb.InspectCandidateResponse)
    assert reply.minimum_version == MIN_CHECKOUT_VERSION
    assert reply.schema_hash == API_SCHEMA_HASH
    assert reply.request_id == "req-1"
    assert reply.selection_revision == 7
    assert reply.state == pb.CandidateState.FOUND
    assert reply.vibesys_checkout is True
    assert reply.vibesys_project is True
    assert reply.git_repository is True
    identity = reply.identity
    assert identity is not None
    assert identity.root == str(root.resolve())
    assert identity.version == MIN_CHECKOUT_VERSION
    assert identity.commit == _COMMIT
    assert identity.compatible is True


def test_a_missing_path_is_not_found_and_an_older_checkout_is_too_old(tmp_path: Path) -> None:
    missing = _result(str(tmp_path / "absent"))
    assert isinstance(missing, pb.InspectCandidateResponse)
    assert missing.state == pb.CandidateState.NOT_FOUND
    assert missing.identity is None
    root = tmp_path / "old"
    root.mkdir()
    _checkout(root, "0.0.9")
    old = _result(str(root))
    assert isinstance(old, pb.InspectCandidateResponse)
    assert old.state == pb.CandidateState.TOO_OLD
    assert old.identity is not None
    assert old.identity.version == "0.0.9"


def test_an_unusable_path_is_invalid_input() -> None:
    failure = _result("bad\npath")
    assert isinstance(failure, OperationFailure)
    assert failure.category == FailureCategory.INVALID_INPUT


@pytest.mark.parametrize(
    "declaration",
    [
        '[project]\nname = "vibesys"\nversion = 1\n',
        '[project]\nname = "vibesys"\nversion = "0.1.1"\nversoin = "9.0"\n',
        '[project]\nname = "vibesys"\nversion = "0.1.1"\n[unknown]\nx = 1\n',
        'project = "vibesys"\n',
    ],
)
def test_invalid_checkout_declarations_are_not_found(tmp_path: Path, declaration: str) -> None:
    (tmp_path / "pyproject.toml").write_text(declaration, encoding="utf-8")
    reply = _result(str(tmp_path))
    assert isinstance(reply, pb.InspectCandidateResponse)
    assert reply.state == pb.CandidateState.NOT_FOUND
    assert reply.vibesys_checkout is False
    assert reply.identity is None


@pytest.mark.parametrize("component", [".vibesys", ".vibesys/tasks"])
def test_symlinked_project_layout_is_invalid_input(tmp_path: Path, component: str) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    _checkout(root, MIN_CHECKOUT_VERSION)
    path = root / component
    moved = tmp_path / "moved"
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=True)
    reply = _result(str(root))
    assert isinstance(reply, OperationFailure)
    assert reply.category == FailureCategory.INVALID_INPUT
    assert "symlink" in reply.message


def test_standard_pyproject_metadata_does_not_hide_a_checkout(tmp_path: Path) -> None:
    _checkout(tmp_path, MIN_CHECKOUT_VERSION)
    with (tmp_path / "pyproject.toml").open("a", encoding="utf-8") as handle:
        handle.write(
            'description = "checkout"\ndependencies = ["pydantic"]\n'
            '[project.scripts]\nvibesys = "entrypoints.cli:main"\n'
            '[build-system]\nrequires = ["hatchling"]\n'
            "[tool.custom]\noption = true\n"
            '[dependency-groups]\ndev = ["pytest"]\n'
        )
    reply = _result(str(tmp_path))
    assert isinstance(reply, pb.InspectCandidateResponse)
    assert reply.state == pb.CandidateState.FOUND
