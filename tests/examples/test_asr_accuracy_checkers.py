"""The speech-to-text examples' accuracy checkers judge a running server over HTTP.

The Modal run environment runs a task's accuracy command inside the serving container,
next to the running server, from a directory that holds only what the command names
(#4102). These tests run each checker the same way: from a copy of the directories its
command names, against an in-memory OpenAI-compatible transcription endpoint.
"""

from __future__ import annotations

import ast
import email.parser
import email.policy
import hashlib
import importlib.util
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.examples.example_task import load_example_task, task_root

from vs_sandbox.api.command_translation import PROJECT_ROOT_TOKEN

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

_EXAMPLES = Path(__file__).parents[2] / "examples" / "model-serving"
_ENDPOINT = "/v1/audio/transcriptions"


class _Case(NamedTuple):
    project: Path
    task: str
    threshold: float
    """The checker's pass criterion: the word-set overlap a non-exact transcript needs."""
    form_fields: tuple[tuple[str, str], ...]
    """What the task's benchmark sends with each clip, so both exercise the same request."""


_CASES = [
    pytest.param(
        _Case(_EXAMPLES / "moonshine-streaming", task, 0.7, (("model", "moonshine"),)),
        id=f"moonshine-{task}",
    )
    for task in ("from-scratch", "vllm")
] + [
    pytest.param(
        _Case(
            _EXAMPLES / "whisper-large-v3",
            "default",
            0.9,
            (("model", "whisper-large-v3"), ("response_format", "json"), ("temperature", "0.0")),
        ),
        id="whisper",
    )
]


# --- an OpenAI-compatible transcription endpoint, in memory ------------------


@dataclass(frozen=True)
class _Request:
    path: str
    fields: dict[str, str]
    filename: str
    media_type: str
    audio: bytes


class HttpError(OSError):
    """A non-2xx answer; the checkers' real transport raises urllib's HTTPError, an OSError."""


@dataclass
class FakeTranscriptionServer:
    """``POST /v1/audio/transcriptions`` as the checkers' ``post`` callable.

    ``answer`` maps the uploaded audio to the response body, or to an ``OSError`` that the
    real transport would raise. Every request is parsed as multipart/form-data and kept.
    """

    answer: Callable[[bytes], bytes | OSError]
    requests: list[_Request] = field(default_factory=list)

    def __call__(self, url: str, body: bytes, content_type: str) -> bytes:
        path = urlsplit(url).path
        if path != _ENDPOINT:
            message = f"HTTP Error 404: Not Found ({path})"
            raise HttpError(message)
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + body
        )
        fields: dict[str, str] = {}
        upload: tuple[str, str, bytes] | None = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            assert isinstance(name, str)
            assert isinstance(payload, bytes)
            filename = part.get_filename()
            if filename is None:
                fields[name] = payload.decode()
            else:
                assert name == "file"
                upload = (filename, part.get_content_type(), payload)
        assert upload is not None, "the request uploads no file part"
        self.requests.append(_Request(path, fields, *upload))
        answer = self.answer(upload[2])
        if isinstance(answer, OSError):
            raise answer
        return answer


def _transcript(text: str) -> bytes:
    return json.dumps({"text": text}).encode()


# --- loading a checker -------------------------------------------------------


def _load_checker(path: Path) -> ModuleType:
    name = "asr_checker_" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@cache
def _checker(case: _Case) -> ModuleType:
    return _load_checker(task_root(case.project, case.task) / "accuracy_checker" / "checker.py")


@cache
def _references(case: _Case) -> dict[str, str]:
    """The stored reference transcript of each clip, by the clip's sha256."""
    path = task_root(case.project, case.task) / "accuracy_checker" / "references.json"
    return {clip["sha256"]: clip["text"] for clip in json.loads(path.read_text())["clips"]}


def _audio_dir(case: _Case) -> str:
    return str(task_root(case.project, case.task) / "test_audio")


def _correct_answer(case: _Case) -> Callable[[bytes], bytes | OSError]:
    references = _references(case)
    return lambda audio: _transcript(references[hashlib.sha256(audio).hexdigest()])


def _stage_accuracy_command(case: _Case, stage: Path) -> tuple[Path, list[str]]:
    """Copy what the Modal evaluator stages for the task's accuracy command into ``stage``.

    It stages, at its project-relative path, the parent directory of each file the command
    names and each directory it names, and runs the command from there. Returns the staged
    script and its arguments, with ``${PROJECT_ROOT}`` as the stage, the working directory.
    """
    command = load_example_task(case.project, case.task).manifest.accuracy.command
    assert command is not None
    for argument in command:
        if argument.startswith(f"{PROJECT_ROOT_TOKEN}/"):
            source = case.project / argument.removeprefix(f"{PROJECT_ROOT_TOKEN}/")
            directory = source if source.is_dir() else source.parent
            target = stage / directory.relative_to(case.project)
            shutil.copytree(directory, target, dirs_exist_ok=True)
    [script_at] = [i for i, argument in enumerate(command) if argument.endswith(".py")]
    script = stage / command[script_at].replace(f"{PROJECT_ROOT_TOKEN}/", "")
    return script, [
        argument.replace(PROJECT_ROOT_TOKEN, ".") for argument in command[script_at + 1 :]
    ]


# --- the checkers -------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES)
def test_a_correct_server_passes_from_only_the_directories_the_command_names(
    case: _Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, arguments = _stage_accuracy_command(case, tmp_path)
    monkeypatch.chdir(tmp_path)
    server = FakeTranscriptionServer(_correct_answer(case))

    assert _load_checker(script).main(arguments, post=server) == 0

    sent = sorted(hashlib.sha256(request.audio).hexdigest() for request in server.requests)
    assert sent == sorted(_references(case))
    for request in server.requests:
        assert request.fields == dict(case.form_fields)
        assert request.media_type == "audio/wav"


@pytest.mark.parametrize("case", _CASES)
def test_the_checker_needs_only_the_standard_library(case: _Case) -> None:
    """It runs on whatever Python the serving container has, with nothing installed."""
    path = task_root(case.project, case.task) / "accuracy_checker" / "checker.py"
    imported = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
    assert imported - {"__future__"} <= sys.stdlib_module_names


@pytest.mark.parametrize("case", _CASES)
def test_the_task_tells_the_agents_that_the_evaluator_uses_the_http_api(case: _Case) -> None:
    assert load_example_task(case.project, case.task).manifest.agent.interface == "service"


def _words(text: str) -> list[str]:
    """The words the checkers compare: lowercase, punctuation dropped."""
    return re.sub(r"[^\w\s]", "", text.lower()).split()


_RESTYLE = st.lists(st.sampled_from([str.upper, str.lower, str.title, lambda w: f"{w},"]))


@st.composite
def _server_transcript(draw: st.DrawFn, reference: str, threshold: float) -> tuple[str, bool]:
    """A transcript of one clip and whether the checker's criterion accepts it.

    Either the reference restyled (case, punctuation, spacing), which matches exactly, or
    the reference without the ``removed`` subset of its ``n`` distinct words and with
    ``added`` new words, whose word-set overlap with the reference is
    ``(n - len(removed)) / (n + len(added))``.
    """
    words = _words(reference)
    if draw(st.booleans()):
        styles = draw(_RESTYLE)
        restyled = [styles[i % len(styles)](w) if styles else w for i, w in enumerate(words)]
        return draw(st.sampled_from([" ", "  ", "\n"])).join(restyled), True
    distinct = sorted(set(words))
    removed = set(draw(st.lists(st.sampled_from(distinct), unique=True)))
    added = [f"zz{i}novel" for i in range(draw(st.integers(0, len(distinct))))]
    kept = [w for w in words if w not in removed]
    overlap = (len(distinct) - len(removed)) / (len(distinct) + len(added))
    exact = not removed and not added
    return " ".join(kept + added), exact or (bool(kept + added) and overlap >= threshold)


@pytest.mark.parametrize("case", _CASES)
@given(data=st.data())
def test_the_gate_passes_exactly_when_every_transcript_meets_the_criterion(
    case: _Case, data: st.DataObject
) -> None:
    answers = {
        digest: data.draw(_server_transcript(reference, case.threshold), label=digest[:8])
        for digest, reference in _references(case).items()
    }
    server = FakeTranscriptionServer(
        lambda audio: _transcript(answers[hashlib.sha256(audio).hexdigest()][0])
    )

    status = _checker(case).main(["--audio-dir", _audio_dir(case)], post=server)

    assert status == (0 if all(accepted for _, accepted in answers.values()) else 1)


_FAILURES: dict[str, bytes | OSError] = {
    "http-500": HttpError("HTTP Error 500: Internal Server Error"),
    "connection-refused": ConnectionRefusedError(111, "Connection refused"),
    "not-json": b"<html>Bad Gateway</html>",
    "no-text": json.dumps({"error": "busy"}).encode(),
    "text-not-a-string": json.dumps({"text": ["hello"]}).encode(),
    "not-an-object": json.dumps(["hello"]).encode(),
}


@pytest.mark.parametrize("case", _CASES)
@given(failure=st.sampled_from(sorted(_FAILURES)), data=st.data())
def test_a_failed_request_fails_the_gate_and_the_other_clips_are_still_checked(
    case: _Case, failure: str, data: st.DataObject
) -> None:
    failing = data.draw(st.sampled_from(sorted(_references(case))))
    correct = _correct_answer(case)
    server = FakeTranscriptionServer(
        lambda audio: (
            _FAILURES[failure] if hashlib.sha256(audio).hexdigest() == failing else correct(audio)
        )
    )

    assert _checker(case).main(["--audio-dir", _audio_dir(case)], post=server) == 1
    assert len(server.requests) == len(_references(case))


@pytest.mark.parametrize("case", _CASES)
def test_a_clip_that_is_not_the_one_the_references_were_made_from_stops_the_check(
    case: _Case, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audio = tmp_path / "test_audio"
    shutil.copytree(_audio_dir(case), audio)
    clip = sorted(audio.glob("*.wav"))[0]
    clip.write_bytes(clip.read_bytes() + b"\0")
    server = FakeTranscriptionServer(_correct_answer(case))

    assert _checker(case).main(["--audio-dir", str(audio)], post=server) == 1

    assert not server.requests
    assert clip.name in capsys.readouterr().out
