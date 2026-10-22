"""One canonical project root, and resume using the recorded one."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_project.api import ProjectRootNotFoundError, canonical_project_root, resume_directory
from vs_sim.api.testing import examples


@pytest.mark.regression
@given(name=st.from_regex(r"[a-z][a-z0-9]{0,7}", fullmatch=True))
@settings(max_examples=examples(pr=6, nightly=40))
@example(name="proj")
def test_user_root_spellings_record_one_directory_and_resume_uses_it(name: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        home = root / "home"
        target = home / name
        target.mkdir(parents=True)
        link = root / "link"
        link.symlink_to(target, target_is_directory=True)
        forms = (f"~/{name}", f"~/{name}/", name, f"{name}/", str(link), str(target))
        recorded = {canonical_project_root(form, base=home, home=home) for form in forms}
        assert recorded == {target.resolve()}
        canonical = target.resolve()
        for form in forms:
            assert resume_directory(str(canonical), form, base=home, home=home) == canonical
            assert resume_directory(None, form, base=home, home=home) == canonical


def test_missing_project_root_names_the_field(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ProjectRootNotFoundError, match="project_root"):
        canonical_project_root(missing, base=tmp_path, home=tmp_path)
    file_path = tmp_path / "file"
    file_path.write_text("x")
    with pytest.raises(ProjectRootNotFoundError, match="project_root"):
        canonical_project_root(file_path, base=tmp_path, home=tmp_path)
