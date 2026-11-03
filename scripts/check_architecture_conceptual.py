"""Keep implementation citations out of the conceptual architecture page."""

import re
from pathlib import Path

PAGE = Path("docs/contributing/architecture.md")
RULES = (
    (
        re.compile(r"`(?:[\w.]+\.)?(?:test_[A-Za-z0-9_]+|Test[A-Z][A-Za-z0-9_]*)`"),
        "test identifier",
    ),
    (re.compile(r"(?<![\w/])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9_]+:\d+\b"), "path:line citation"),
    (re.compile(r"`[\w.]+\([^`\n()]*\S[^`\n()]*\)`"), "call signature with arguments"),
)


def violations(text: str) -> list[str]:
    """Return actionable diagnostics for each forbidden form on each line."""
    return [
        f"{PAGE}:{number}: {reason}; architecture must stay conceptual; describe the role or rule instead"
        for number, line in enumerate(text.splitlines(), 1)
        for pattern, reason in RULES
        if pattern.search(line)
    ]


def main() -> int:
    """Check the architecture page and return a process status."""
    errors = violations(PAGE.read_text(encoding="utf-8"))
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
