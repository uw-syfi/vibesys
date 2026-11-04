"""Local checks-only must run CI's required contract and static architecture checks."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_proto_verification_is_required_in_local_checks_only() -> None:
    checks = tomllib.loads((ROOT / ".repoctl/checks.toml").read_text(encoding="utf-8"))
    group = next(item for item in checks["check_groups"] if item["name"] == "proto")

    assert group.get("include_in_checks"), "checks-only must run CI's proto group"
    assert not group.get("local_skip"), "proto verification must fail rather than skip locally"
    assert group["trigger_job"] == "proto"
    assert group["commands"] == [["./scripts/check_proto.sh"]]


def test_architecture_scans_are_required_in_local_checks_only() -> None:
    checks = tomllib.loads((ROOT / ".repoctl/checks.toml").read_text(encoding="utf-8"))
    groups = {item["name"]: item for item in checks["check_groups"]}
    assert "python_architecture" in groups, "checks-only must select the architecture scans"
    group = groups["python_architecture"]

    assert group.get("include_in_checks"), "checks-only must run the static architecture scans"
    assert not group.get("local_skip"), "architecture scans must not skip locally"
    assert group["trigger_job"] == "python"
    commands = [command for command in group["commands"] if "tests/architecture" in command]
    assert commands, "checks-only must run the architecture scans"
    (command,) = commands
    assert "--no-cov" in command, "static scans must stay cheap locally"
    excluded = {arg.removeprefix("--ignore=") for arg in command if arg.startswith("--ignore=")}
    scans = {
        str(path.relative_to(ROOT)) for path in (ROOT / "tests/architecture").glob("test_*.py")
    }
    assert excluded < scans
    assert "tests/architecture/test_toml_config_models.py" not in excluded
    assert "tests/architecture/test_project_state_encapsulation.py" not in excluded
    assert excluded == {
        "tests/architecture/test_stack_evaluator.py",
        "tests/architecture/test_ordered_map_evaluator.py",
        "tests/architecture/test_unordered_map_evaluator.py",
        "tests/architecture/test_unix_socket.py",
        "tests/architecture/test_delegated_merge.py",
        "tests/architecture/test_distribution_packaging.py",
        "tests/architecture/test_prompt_resource_contract.py",
    }, "only runtime, native-build, and packaging tests may be excluded"
