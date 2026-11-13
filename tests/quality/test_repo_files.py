"""Regression coverage for repository file discovery that must ignore submodules.

See uw-syfi/vibesys#3890: check scripts that walked the working tree with
``Path.rglob`` / ``Path.walk`` also read checked-out submodule contents, so
local checks failed where CI (no submodule checkout) passed. Discovery now
goes through ``scripts.repo_files``, which lists ``git ls-files`` only.
"""

from __future__ import annotations

import ast
import configparser
from pathlib import Path

import pytest
from scripts.check_contract_sot import ROOTS, sources
from scripts.check_file_length import measure
from scripts.check_lint_waivers import discover_ruff_files
from scripts.repo_files import tracked_files, tracked_paths

from vs_project.api import Git
from vs_sim.api import OsProcesses

_GIT = Git(OsProcesses())
REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_NAME = "__vibesys_scanner_probe_3890__.py"
SCANNER_MODULES = (
    REPO_ROOT / "scripts" / "check_lint_waivers.py",
    REPO_ROOT / "scripts" / "check_contract_sot.py",
    REPO_ROOT / "scripts" / "check_file_length.py",
    REPO_ROOT / "scripts" / "check_purity.py",
    REPO_ROOT / "scripts" / "check_test_isolation.py",
    REPO_ROOT / "scripts" / "check_member_dependencies.py",
    REPO_ROOT / "scripts" / "flow_code.py",
)


def _gitmodules_paths(root: Path) -> tuple[str, ...]:
    parser = configparser.ConfigParser()
    parser.read(root / ".gitmodules")
    return tuple(sorted({parser[section]["path"] for section in parser.sections()}))


def _calls_repository_tree_walk(tree: ast.AST) -> list[str]:
    """Return attribute names used for recursive repository walks.

    ``ast.walk`` is an AST traversal and is allowed. ``Path.rglob``, ``Path.walk``,
    ``os.walk``, and ``glob(..., recursive=True)`` are not.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "rglob":
            found.append("rglob")
        elif node.func.attr == "walk":
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "ast":
                continue
            found.append("walk")
        elif node.func.attr == "glob" and any(
            keyword.arg == "recursive"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            found.append("glob(recursive=True)")
    return found


def test_tracked_files_omit_paths_absent_from_the_listing(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "owned.py").write_text("x = 1\n", encoding="utf-8")
    nested = tmp_path / "examples" / "vendor"
    nested.mkdir(parents=True)
    (nested / "intruder.py").write_text("y = 2\n", encoding="utf-8")
    (nested / "later.py").write_text("z = 3\n", encoding="utf-8")

    relative = {
        path.relative_to(tmp_path).as_posix()
        for path in tracked_files(
            tmp_path,
            suffixes=(".py",),
            names=("src/owned.py", "examples/vendor/intruder.py"),
        )
    }

    assert relative == {"src/owned.py", "examples/vendor/intruder.py"}
    assert "examples/vendor/later.py" not in relative


def test_tracked_paths_match_git_ls_files_on_this_repository() -> None:
    listing = _GIT.run_text(["ls-files", "-z"], cwd=REPO_ROOT)
    listing.check()
    assert tracked_paths(REPO_ROOT) == tuple(path for path in listing.stdout.split("\0") if path)


def test_submodule_probes_are_invisible_to_ported_scanners() -> None:
    probes: list[Path] = []
    try:
        for relative in _gitmodules_paths(REPO_ROOT):
            directory = REPO_ROOT / relative
            if not directory.is_dir():
                continue
            probe = directory / PROBE_NAME
            probe.write_text("# probe for #3890\n", encoding="utf-8")
            probes.append(probe)

        if not probes:
            pytest.skip("no checked-out submodules to plant probes under")

        lint_files, error = discover_ruff_files(REPO_ROOT)
        assert error is None
        lint_set = {path.resolve() for path in lint_files}
        contract_set = {REPO_ROOT / source.path for source in sources(REPO_ROOT)}
        length_set = {
            REPO_ROOT / relative for relative in measure(REPO_ROOT, ("src", "libs", "scripts"))
        }
        tracked_set = set(tracked_files(REPO_ROOT, suffixes=(".py",)))

        for probe in probes:
            assert probe.resolve() not in lint_set
            assert probe.resolve() not in contract_set
            assert probe.resolve() not in length_set
            assert probe.resolve() not in tracked_set
            assert probe.relative_to(REPO_ROOT).as_posix() not in tracked_paths(REPO_ROOT)
    finally:
        for probe in probes:
            probe.unlink(missing_ok=True)


def test_scanner_scripts_do_not_walk_the_tree_outside_repo_files() -> None:
    offenders: list[str] = []
    for path in SCANNER_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        walks = _calls_repository_tree_walk(tree)
        if walks:
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {', '.join(walks)}")
    assert offenders == []


def test_contract_discovery_ignores_checked_out_example_submodules() -> None:
    submodule = "examples/microservices/repositories/deathstarbench"
    if not (REPO_ROOT / submodule).is_dir():
        pytest.skip(f"{submodule} is not checked out")
    discovered = {source.path for source in sources(REPO_ROOT)}
    leaked = sorted(path for path in discovered if path.startswith(f"{submodule}/"))
    assert leaked == []
    assert any(path.startswith("examples/") for path in discovered)


def test_ported_python_discovery_matches_rglob_minus_submodules() -> None:
    """Discovered sets equal a filtered rglob baseline aside from submodule paths."""
    submodule_prefixes = tuple(f"{path.rstrip('/')}/" for path in _gitmodules_paths(REPO_ROOT))

    def is_submodule_content(relative: str) -> bool:
        return any(relative.startswith(prefix) for prefix in submodule_prefixes)

    tracked_index = set(tracked_paths(REPO_ROOT))
    rglob_py = {
        path.relative_to(REPO_ROOT).as_posix()
        for directory in ROOTS
        for path in (REPO_ROOT / directory).rglob("*.py")
        if path.is_file()
        and not {"__pycache__", ".venv", "node_modules", ".git"}.intersection(
            path.relative_to(REPO_ROOT).parts
        )
        and not is_submodule_content(path.relative_to(REPO_ROOT).as_posix())
    }
    tracked_py = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in tracked_files(REPO_ROOT, suffixes=(".py",), under=ROOTS)
    }
    assert tracked_py == {path for path in rglob_py if path in tracked_index}
