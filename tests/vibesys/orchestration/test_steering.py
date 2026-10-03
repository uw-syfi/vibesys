"""Tests for policy-owned operator steering rendering."""

from vibesys.steering import splice_steering


def test_empty_steering_preserves_the_prompt() -> None:
    assert splice_steering("Do the work", []) == "Do the work"


def test_steering_appends_the_reviewed_operator_block() -> None:
    spliced = splice_steering(
        "Do the work  ",
        ["focus on latency", "check reward hacking"],
    )

    assert spliced == (
        "Do the work\n\n"
        "## Operator steering (live)\n\n"
        "The operator sent the following instruction(s) for this invocation. "
        "Treat them as high-priority guidance for the work you do now:\n\n"
        "- focus on latency\n"
        "- check reward hacking\n"
    )
