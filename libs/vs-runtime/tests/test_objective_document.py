"""Public contract for objective document materialization."""

from pathlib import Path

import pytest

from vs_runtime.api.infrastructure import materialize_objective_document


def test_materializes_exact_objective_at_the_selected_destination(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    destination = tmp_path / "runtime" / "effective-objective.md"
    destination.parent.mkdir()
    objective = "Optimize the service.\n\n- Preserve BF16.\n"

    result = materialize_objective_document(
        objective,
        workspace=workspace,
        authored_document=None,
        destination=destination,
    )

    assert result == destination
    assert destination.read_text() == objective


def test_returns_an_exact_authored_document_inside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    document = workspace / "docs" / "OBJECTIVE.md"
    document.parent.mkdir(parents=True)
    document.write_text("goal\n")
    unused_destination = tmp_path / "runtime" / "effective-objective.md"

    result = materialize_objective_document(
        "goal\n",
        workspace=workspace,
        authored_document=document,
        destination=unused_destination,
    )

    assert result == document.resolve()
    assert not unused_destination.exists()


def test_rejects_an_authored_document_that_resolves_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "OBJECTIVE.md"
    outside.write_text("goal")
    authored_document = workspace / "OBJECTIVE.md"
    authored_document.symlink_to(outside)

    with pytest.raises(ValueError, match="must be inside the project workspace"):
        materialize_objective_document(
            "goal",
            workspace=workspace,
            authored_document=authored_document,
            destination=tmp_path / "unused.md",
        )


@pytest.mark.parametrize("document_state", ["missing", "directory", "mismatch"])
def test_rejects_an_authored_document_without_exact_file_content(
    tmp_path: Path,
    document_state: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    document = workspace / "OBJECTIVE.md"
    if document_state == "directory":
        document.mkdir()
    elif document_state == "mismatch":
        document.write_text("stale goal")

    with pytest.raises(ValueError, match="does not match its committed document"):
        materialize_objective_document(
            "goal",
            workspace=workspace,
            authored_document=document,
            destination=tmp_path / "unused.md",
        )
