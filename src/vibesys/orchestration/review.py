"""Review outcomes shared by built-in orchestration policies."""

from enum import StrEnum


class Verdict(StrEnum):
    """Binary outcome returned by a policy review or validation stage."""

    PASS = "pass"  # noqa: S105  # lint-waiver: LW-010203 [S105]; this is a public result enum value, not a credential.
    FAIL = "fail"


__all__ = ["Verdict"]
