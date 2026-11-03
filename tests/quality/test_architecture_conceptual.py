"""The architecture policy detects implementation details, not names."""

import pytest
from scripts.check_architecture_conceptual import violations


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("`test_foo`", "test identifier"),
        ("`pkg.TestFoo`", "test identifier"),
        ("src/file.py:123", "path:line citation"),
        ("`pkg.Func(args)`", "call signature with arguments"),
    ],
)
def test_reports_implementation_details(text: str, reason: str) -> None:
    result = violations(f"first line\n{text}\n")
    assert len(result) == 1
    assert ":2:" in result[0]
    assert reason in result[0]
    assert "conceptual" in result[0]


@pytest.mark.parametrize("name", ["package", "pkg.module", "vibesys-host", "Test", "pkg.Func()"])
def test_allows_names_and_empty_calls(name: str) -> None:
    assert violations(f"`{name}`") == []
