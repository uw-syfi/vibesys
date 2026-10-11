"""The bundle's fixed profiling workload exercises serving without benchmark gates."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

_SPEC = importlib.util.spec_from_file_location(
    "bundle_profile", Path(__file__).with_name("profile.py")
)
assert _SPEC is not None and _SPEC.loader is not None
profile = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = profile
_SPEC.loader.exec_module(profile)


class FakeCompletionClient:
    """Completion endpoint that enforces the bundle's required request interface."""

    def __init__(self, text: str = "", *, serves: bool = True) -> None:
        self.text = text
        self.serves = serves
        self.requests: list[dict[str, Any]] = []

    def complete(self, request: Any) -> Any:
        payload = request.model_dump()
        assert set(payload) == {"model", "prompt", "max_tokens", "temperature", "ignore_eos"}
        assert payload["model"] == profile.MODEL
        assert payload["max_tokens"] == profile.OUTPUT_TOKENS
        assert payload["ignore_eos"] is True
        self.requests.append(payload)
        if not self.serves:
            raise ConnectionError("server never served")
        return profile.ProfileCompletionResponse.model_validate(
            {
                "choices": [{"text": self.text}],
                "usage": {"completion_tokens": payload["max_tokens"]},
            }
        )


@given(text=st.text())
def test_serving_candidates_provide_fixed_decode_load_without_a_throughput_gate(text: str) -> None:
    client = FakeCompletionClient(text)
    assert profile.run_profile(client) == len(profile.PROMPTS)
    assert [request["prompt"] for request in client.requests] == list(profile.PROMPTS)


def test_a_candidate_that_never_serves_does_not_complete_its_profiling_load() -> None:
    with pytest.raises(ConnectionError, match="never served"):
        profile.run_profile(FakeCompletionClient(serves=False))


@given(count=st.integers(max_value=0))
def test_a_response_without_decoded_tokens_is_not_serving_evidence(count: int) -> None:
    with pytest.raises(ValidationError, match="completion_tokens"):
        profile.ProfileCompletionResponse.model_validate(
            {"choices": [{"text": ""}], "usage": {"completion_tokens": count}}
        )


@pytest.mark.parametrize("choices", [[], [None], [{"text": None}], "not choices"])
def test_malformed_completion_choices_are_not_serving_evidence(choices: Any) -> None:
    with pytest.raises(ValidationError, match="choices"):
        profile.ProfileCompletionResponse.model_validate(
            {"choices": choices, "usage": {"completion_tokens": 16}}
        )
