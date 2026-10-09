"""`scripts/smoke_dynamic_loop.sh` keeps its pytest base directory outside the checkout.

The smoke's runs create input projects under pytest's base directory, and a
project nested in another Git repository is rejected. A base directory inside
the checkout (the old `.logs/smoke-<timestamp>` default) therefore fails the
run, so the script must default to a location outside it and refuse an
explicit one inside it.

A fake `uv` placed first on PATH records the `--basetemp` argument the script
would hand to pytest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "smoke_dynamic_loop.sh"

FAKE_UV = """#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    --basetemp=*) printf '%s' "${arg#--basetemp=}" > "$FAKE_UV_RECORD" ;;
  esac
done
"""


def run_script(
    tmp_path: Path, *, tmpdir: Path | None, smoke_dir: Path | None
) -> tuple[subprocess.CompletedProcess[str], Path | None]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "uv"
    fake.write_text(FAKE_UV, encoding="utf-8")
    fake.chmod(0o755)
    record = tmp_path / "basetemp.txt"
    record.unlink(missing_ok=True)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "FAKE_UV_RECORD": str(record),
    }
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    if smoke_dir is not None:
        env["VIBESYS_SMOKE_DIR"] = str(smoke_dir)
    # test-isolation: run the repository script itself against a test-owned fake uv.
    result = subprocess.run(  # noqa: S603  # lint-waiver: LW-139401 [S603]; fixed repository script argv, no shell, test-owned environment
        [str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, (Path(record.read_text(encoding="utf-8")) if record.exists() else None)


def is_inside(path: Path, root: Path) -> bool:
    return root.resolve() in path.resolve().parents


def test_default_basetemp_is_a_fresh_directory_under_tmpdir_outside_the_checkout(
    tmp_path: Path,
) -> None:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()

    result, basetemp = run_script(tmp_path, tmpdir=tmpdir, smoke_dir=None)

    assert result.returncode == 0, result.stderr
    assert basetemp is not None
    assert basetemp.is_dir()
    assert basetemp.parent == tmpdir.resolve()
    assert not is_inside(basetemp, REPO_ROOT)


def test_two_default_runs_do_not_share_a_basetemp(tmp_path: Path) -> None:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()

    _, first = run_script(tmp_path, tmpdir=tmpdir, smoke_dir=None)
    _, second = run_script(tmp_path, tmpdir=tmpdir, smoke_dir=None)

    assert first is not None
    assert second is not None
    assert first != second


@settings(max_examples=8, deadline=None)
@given(
    parts=st.lists(st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True), min_size=0, max_size=3)
)
def test_a_smoke_dir_inside_the_checkout_is_rejected_before_pytest_runs(
    parts: list[str],
) -> None:
    target = REPO_ROOT / ".logs" / "smoke-test-nesting" / Path(*parts)
    with tempfile.TemporaryDirectory() as scratch:
        try:
            result, basetemp = run_script(Path(scratch), tmpdir=None, smoke_dir=target)
        finally:
            # The script creates the rejected directory before checking it.
            shutil.rmtree(REPO_ROOT / ".logs" / "smoke-test-nesting", ignore_errors=True)

    assert result.returncode != 0
    assert basetemp is None
    assert "inside the checkout" in result.stderr


def test_an_explicit_smoke_dir_outside_the_checkout_is_used_as_given(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen"

    result, basetemp = run_script(tmp_path, tmpdir=None, smoke_dir=chosen)

    assert result.returncode == 0, result.stderr
    assert basetemp == chosen.resolve()
