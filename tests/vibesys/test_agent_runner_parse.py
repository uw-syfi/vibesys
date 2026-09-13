"""Recovery of a typed reply from raw model text (``parse_typed_response_text``).

Two failure shapes seen in real runs: narrative text with inline JSON-looking
braces before the reply object, and string values containing raw newlines
instead of ``\\n`` escapes (Kimi K3 through OpenRouter emits both).
"""

from __future__ import annotations

from pydantic import BaseModel

from vibesys.agent_runner import parse_typed_response_text


class Reply(BaseModel):
    analysis: str
    verdict: str


def test_plain_and_fenced_objects_still_parse() -> None:
    plain = '{"analysis": "ok", "verdict": "pass"}'
    fenced = "Here you go:\n```json\n" + plain + "\n```"
    assert parse_typed_response_text(plain, Reply) == Reply(analysis="ok", verdict="pass")
    assert parse_typed_response_text(fenced, Reply) == Reply(analysis="ok", verdict="pass")


def test_reply_after_inline_braces_in_narrative_is_found() -> None:
    text = (
        'I set {"cudagraph_mode": "FULL_DECODE_ONLY"} in serve.sh, which crashed on '
        'a shape {1, 2}. Final answer:\n{"analysis": "knob flipped", "verdict": "pass"}'
    )
    assert parse_typed_response_text(text, Reply) == Reply(analysis="knob flipped", verdict="pass")


def test_raw_newlines_inside_string_values_are_repaired() -> None:
    text = '{"analysis": "line one\nline two\twith tab", "verdict": "fail"}'
    assert parse_typed_response_text(text, Reply) == Reply(
        analysis="line one\nline two\twith tab", verdict="fail"
    )


def test_escaped_quotes_and_backslashes_survive_the_repair() -> None:
    text = '{"analysis": "said \\"hi\\"\nthen C:\\\\path", "verdict": "pass"}'
    parsed = parse_typed_response_text(text, Reply)
    assert parsed == Reply(analysis='said "hi"\nthen C:\\path', verdict="pass")


def test_nothing_parseable_returns_none() -> None:
    assert parse_typed_response_text("no json here {unbalanced", Reply) is None
    assert parse_typed_response_text("", Reply) is None
