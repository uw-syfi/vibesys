"""Strict JSON response parsing through the public agent client."""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict

from vs_agent.api import AgentClient
from vs_agent.api.testing import FakeDriver


class _IssueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    issue_id: int
    summary: str
    files_touched: tuple[str, ...] = ()
    self_check: str


FALLBACK = _IssueResponse(issue_id=0, summary="fallback", self_check="invalid response")


def _invoke(text: str) -> _IssueResponse:
    client = AgentClient(FakeDriver(answer=text))
    try:
        return client.invoke(
            kind="implementer",
            workspace=Path.cwd(),
            system_prompt="Return the issue response as JSON.",
            user_prompt="Inspect the candidate.",
            response_cls=_IssueResponse,
            fallback_factory=lambda: FALLBACK,
            round_label="structured response",
        )
    finally:
        client.close()


def test_strict_issue_response_accepts_json_array_as_tuple() -> None:
    response = _invoke(
        '{"issue_id":1,"summary":"Already correct","files_touched":[],"self_check":"Inspected README"}'
    )

    assert response == _IssueResponse(
        issue_id=1, summary="Already correct", files_touched=(), self_check="Inspected README"
    )


@given(
    issue_id=st.integers(),
    files=st.lists(st.text(), max_size=8),
    wrapper=st.sampled_from(["{}", "```json\n{}\n```", "Response:\n{}\nDone."]),
)
def test_strict_json_responses_round_trip_with_supported_wrappers(
    issue_id: int, files: list[str], wrapper: str
) -> None:
    expected = _IssueResponse(
        issue_id=issue_id, summary="Inspected", files_touched=tuple(files), self_check="Checked"
    )

    assert _invoke(wrapper.format(expected.model_dump_json())) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no JSON response",
        "{invalid JSON}",
        '{"issue_id":"1","summary":"ok","self_check":"checked"}',
        '{"issue_id":1,"summary":"ok","files_touched":[2],"self_check":"checked"}',
        '{"issue_id":1,"summary":"ok","self_check":"checked","unknown":true}',
    ],
)
def test_invalid_strict_responses_keep_the_documented_fallback(text: str) -> None:
    assert _invoke(text) == FALLBACK
