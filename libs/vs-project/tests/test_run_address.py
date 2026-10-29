"""Run publication includes its canonical project even before any head exists."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_project.api import Project
from vs_proto.api import RunAddress, validate


@pytest.mark.regression
@example(name="unvisited project")
@given(name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz 0123456789", min_size=1, max_size=40))
def test_published_run_has_a_valid_canonical_project_address(name: str) -> None:
    """Project names, spaces and canonical aliases cannot be lost at publication."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as monkeypatch:
        tmp_path = Path(temporary)
        monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
        root = tmp_path / f"project-{name}"
        root.mkdir(exist_ok=True)
        link = Project.publish_run_for(root, "run-a")
        address = RunAddress.from_binary((tmp_path / "home/run-addresses/run-a.pb").read_bytes())
        validate(address)
        assert address.project_root == str(root.resolve())
        assert address.log_directory == str(link.resolve())
        assert not (link / "run.head").exists()
        assert Project.publish_run_for(root, "run-a") == link
        assert (
            RunAddress.from_binary((tmp_path / "home/run-addresses/run-a.pb").read_bytes())
            == address
        )
