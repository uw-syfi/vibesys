"""Safe translation of semantic path tokens in evaluator command arguments."""

from __future__ import annotations

import json

import pytest

from vs_sandbox.api.command_translation import translate_command_arguments

_REPLACEMENTS = [("${ROOT}", "/work")]


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("plain", "plain"),
        ("${ROOT}/x", "/work/x"),
        ("[not json ${ROOT}", "[not json /work"),
        ('{"k": "${ROOT}"}', '{"k": "/work"}'),
        ('["${ROOT}/a", "b"]', '["/work/a","b"]'),
        ('[1, "${ROOT}"]', '[1, "/work"]'),
    ],
)
def test_translate_command_argument_rewrites_tokens(argument: str, expected: str) -> None:
    assert translate_command_arguments((argument,), _REPLACEMENTS) == (expected,)


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-c", "cat ${ROOT}/f", "arg0"],
        ["env", "-S", "run ${ROOT}"],
        ["python3", "-c", "print('${ROOT}')"],
        ["node", "--eval=console.log('${ROOT}')"],
        ["/usr/bin/env", "--", "python", "-c", "open('${ROOT}')"],
    ],
)
def test_translate_rejects_tokens_inside_executable_source(argv: list[str]) -> None:
    with pytest.raises(ValueError, match="unsafe; pass them as positional arguments"):
        translate_command_arguments((json.dumps(argv),), _REPLACEMENTS)


def test_translate_allows_tokens_after_shell_source() -> None:
    argv = ["bash", "-c", 'cat "$1"', "arg0", "${ROOT}/f"]

    (translated,) = translate_command_arguments((json.dumps(argv),), _REPLACEMENTS)

    assert json.loads(translated) == ["bash", "-c", 'cat "$1"', "arg0", "/work/f"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["bash", "-c", "src", "a"], 2),
        (["sh", "-lc", "src", "a"], 2),
        (["bash", "--norc", "-c", "src"], 3),
        (["bash", "a", "b"], None),
        (["bash", "x", "-c"], None),
        (["bash", "--", "-c", "src"], None),
        (["bash", "-c", "src"], 2),
        (["sh", "script.sh"], None),
        (["bash", "-c"], None),
        (["env", "bash", "-c", "src", "a"], 3),
        (["env", "FOO=1", "-i", "-u", "X", "bash", "-c", "src", "a"], 7),
        (["env", "-S", "cmd args"], 2),
        (["env", "--split-string=cmd args"], 1),
        (["env", "-Scmd"], 1),
        (["env", "-u"], None),
        (["env", "--"], None),
        (["env", "-i"], None),
        (["env", "--", "python3", "-c", "src"], 4),
        (["env", "node", "-e", "src"], 3),
        (["node", "-p"], None),
        (["nodejs", "--print=1"], 1),
        (["node", "app.js"], None),
        (["python3.12", "-c", "src"], 2),
        (["python", "script.py"], None),
        (["pythonx", "-c", "src"], None),
        (["ruby", "-e", "src"], None),
    ],
)
def test_source_detection_controls_translation(argv: list[str], expected: int | None) -> None:
    with_token = list(argv)
    target = expected if expected is not None else len(with_token) - 1
    with_token[target] = f"{with_token[target]} ${{ROOT}}"
    serialized = json.dumps(with_token)

    if expected is not None:
        with pytest.raises(ValueError, match="unsafe; pass them as positional arguments"):
            translate_command_arguments((serialized,), _REPLACEMENTS)
    else:
        (translated,) = translate_command_arguments((serialized,), _REPLACEMENTS)
        assert "${ROOT}" not in translated
        assert "/work" in translated
