import pytest

from vibesys.llm_client import _is_google_model


@pytest.mark.parametrize(
    "model_name, expected",
    [
        ("gemini-1.5-pro", True),
        ("gemma-2b", True),
        ("claude-sonnet-4-6", False),
        ("gpt-4", False),
        ("", False),
        ("Gemini-1.5-pro", False),  # case-sensitive
    ],
)
def test_is_google_model(model_name, expected):
    assert _is_google_model(model_name) is expected
