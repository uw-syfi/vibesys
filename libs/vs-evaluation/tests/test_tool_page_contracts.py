"""Public contracts for explicit bounded-tool read modes."""

import pytest
from pydantic import ValidationError

from vs_evaluation.api import EvidenceArgs, RunOperationsArgs


def test_operation_read_modes_are_exclusive() -> None:
    for left, right in (("cursor", "since"), ("cursor", "reference_id"), ("since", "reference_id")):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            RunOperationsArgs.model_validate({left: "a", right: "b"})


def test_tool_read_modes_reject_unknown_keys() -> None:
    for contract in (EvidenceArgs, RunOperationsArgs):
        with pytest.raises(ValidationError, match="unknown_option"):
            contract.model_validate({"unknown_option": True})
