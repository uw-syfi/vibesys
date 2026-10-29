"""The physical layout establishes routing; recorded roots only prove external keys."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_project.api import Project


@pytest.mark.regression
@example(name="unvisited project", layout="local", recorded="missing", stored=False)
@example(name="moved project", layout="local", recorded="stale", stored=True)
@example(name="external project", layout="external", recorded="correct", stored=True)
@example(name="external project", layout="external", recorded="missing", stored=False)
@example(name="external project", layout="external", recorded="stale", stored=True)
@example(name="external project", layout="external", recorded="relative", stored=False)
@example(name="invalid run", layout="invalid", recorded="correct", stored=False)
@example(name="external project", layout="external", recorded="ancestor", stored=True)
@given(
    name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz 0123456789", min_size=1, max_size=30),
    layout=st.sampled_from(["local", "external", "outside", "invalid"]),
    recorded=st.sampled_from(["missing", "correct", "stale", "relative", "ancestor"]),
    stored=st.booleans(),
)
def test_project_comes_from_the_run_location(
    *, name: str, layout: str, recorded: str, stored: bool
) -> None:
    """Vary several projects, copied directories, saved roots and address presence."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as environment:
        base = Path(temporary)
        environment.setenv("VIBESYS_STATE_HOME", str(base / "home"))
        roots = [base / f"project-{name}", base / "other-project"]
        for project in roots:
            project.mkdir()
        for project in roots:
            directory = Project.publish_run_for(project, "run-a").resolve()
            address = base / "home/run-addresses/run-a.pb"
            if not stored:
                address.unlink()
            if layout == "local":
                directory = project / ".vibesys/state/local/runs/run-a/logs"
                directory.mkdir(parents=True)
            elif layout == "invalid":
                directory = project / ".vibesys/state/local/runs/invalid id/logs"
                directory.mkdir(parents=True)
            elif layout == "outside":
                directory = project / "copied-run"
                directory.mkdir()
            # A saved root from another existing project must never redirect this run.
            saved = {
                "correct": project,
                "stale": roots[1 - roots.index(project)],
                "missing": None,
                "relative": Path("relative"),
                "ancestor": directory.parents[3],
            }[recorded]
            found = Project.from_log_directory(directory, recorded_root=saved)
            if layout == "local" or (layout == "external" and recorded == "correct"):
                assert found is not None
                assert found.root == project.resolve()
            else:
                assert found is None
            assert address.exists() == stored
