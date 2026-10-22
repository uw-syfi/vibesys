"""Local checks-only must run the same required proto check group as CI."""

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
