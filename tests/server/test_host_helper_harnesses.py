"""The host helper's harness choice: ``harnesses.list`` and ``run.start``'s ``harness``.

A run started through the helper uses only a vetted provider and model: the
client picks one of the offered pairs, a pair outside them is refused, and the
terminal's opt-out flag is refused in ``args`` in every spelling argparse
accepts. Requests run through :func:`answer` over the Fakes of
``host_helper_support``; the feature gate through the channel core :func:`step`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from hypothesis import assume, example, given
from hypothesis import strategies as st
from tests.server.host_helper_support import (
    HARNESSES,
    FakeRunLauncher,
    fake_host,
    quiet_machine,
    record,
)

from server.host_api import (
    CLI_PROVIDER_FLAG,
    MODEL_FLAG,
    UNVETTED_MODEL_FLAG,
    HarnessChoice,
    HarnessesListReply,
    HarnessesListRequest,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    RunStartReply,
    RunStartRequest,
)
from server.host_helper import ChannelState, Execute, Received, Send, step
from server.host_helper_requests import answer
from vs_sim.api.testing import SimNetwork, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from server.host_api import HarnessOption, HelperReply


def _project(root: Path) -> Path:
    task = root / ".vibesys" / "tasks" / "spsc"
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("", encoding="utf-8")
    return root


def _start(
    project: Path,
    *,
    args: tuple[str, ...] = (),
    harness: HarnessChoice | None = None,
    harnesses: tuple[HarnessOption, ...] = HARNESSES,
) -> tuple[HelperReply | HelperError, FakeRunLauncher]:
    started = record("0123456789ab", project_root=str(project))
    launcher = FakeRunLauncher(lambda _project, _arguments: started)
    threads = SimThreads()
    host = replace(
        fake_host(threads, SimNetwork(threads), launcher=launcher), harnesses=lambda: harnesses
    )
    request = RunStartRequest(
        request_id="s", deadline_ms=1, project=str(project), task="spsc", args=args, harness=harness
    )
    return answer(request, host, threads.event()), launcher


def _abbreviations(flag: str) -> st.SearchStrategy[str]:
    """Every spelling argparse reads as ``flag``: a prefix of it past ``--``, maybe with a value."""
    prefix = st.integers(len("--") + 1, len(flag)).map(lambda end: flag[:end])
    value = st.one_of(st.none(), st.text(max_size=8))
    return st.builds(lambda name, v: name if v is None else f"{name}={v}", prefix, value)


_PLAIN_ARGS = st.lists(st.sampled_from(("--max-rounds", "3", "--name", "x y")), max_size=3)


# ----- harnesses.list -----


def test_the_list_is_the_hosts_vetted_harnesses() -> None:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    reply = answer(HarnessesListRequest(request_id="l", deadline_ms=1), host, threads.event())
    assert reply == HarnessesListReply(request_id="l", harnesses=HARNESSES)


@example(installed={"claude"})
@given(installed=st.sets(st.sampled_from(["codex", "claude"])))
def test_the_list_is_the_catalog_in_its_own_order_whichever_clis_are_installed(
    installed: set[str],
) -> None:
    """Which CLIs a machine has is ``machine.describe``'s answer (``harness_clis``): telling takes a
    login shell, and a list the app asks for at every launch starts none. A client that wants the
    installed providers first orders the list with that answer.

    Regression: the list asked the machine, so every ``harnesses.list`` started a shell.
    """
    threads = SimThreads()
    host = replace(
        fake_host(threads, SimNetwork(threads)),
        machine=quiet_machine(installed=frozenset(installed)),
    )

    reply = answer(HarnessesListRequest(request_id="l", deadline_ms=1), host, threads.event())

    assert isinstance(reply, HarnessesListReply)
    assert list(reply.harnesses) == list(HARNESSES)


def _negotiated(features: list[str]) -> ChannelState:
    hello = {
        "type": "hello",
        "request_id": "h",
        "deadline_ms": 5000,
        "client_versions": [1, 1],
        "features": features,
        "app_version": "test",
    }
    state, _ = step(ChannelState(), Received(json.dumps(hello).encode(), 0.0), "v")
    return state


def _refusal(state: ChannelState, message: dict[str, Any]) -> HelperError | None:
    _, outputs = step(state, Received(json.dumps(message).encode(), 0.0), "v")
    sent = [output.message for output in outputs if isinstance(output, Send)]
    if any(isinstance(output, Execute) for output in outputs):
        return None
    (refusal,) = sent
    assert isinstance(refusal, HelperError)
    return refusal


def test_the_list_and_a_chosen_harness_need_the_harnesses_feature() -> None:
    listing = {"type": "harnesses.list", "request_id": "l", "deadline_ms": 5000}
    chosen = {
        "type": "run.start",
        "request_id": "s",
        "deadline_ms": 5000,
        "project": "/p",
        "harness": {"provider": "codex", "model": "gpt-a"},
    }
    plain = {"type": "run.start", "request_id": "s", "deadline_ms": 5000, "project": "/p"}
    without = _negotiated([])
    with_feature = _negotiated([HelperFeature.HARNESSES.value])

    for message in (listing, chosen):
        refusal = _refusal(without, message)
        assert refusal is not None
        assert refusal.code is HelperErrorCode.FEATURE_NOT_NEGOTIATED
        assert _refusal(with_feature, message) is None
    assert _refusal(without, plain) is None


# ----- run.start with a harness -----


@given(st.sampled_from(HARNESSES), _PLAIN_ARGS)
def test_an_offered_harness_starts_with_its_provider_and_model(
    tmp_path_factory: pytest.TempPathFactory, option: HarnessOption, args: list[str]
) -> None:
    project = _project(tmp_path_factory.mktemp("proj"))
    choice = HarnessChoice(provider=option.provider, model=option.model)
    reply, launcher = _start(project, args=tuple(args), harness=choice)
    assert isinstance(reply, RunStartReply)
    (call,) = launcher.calls
    assert call == (
        project.resolve(),
        (
            "--detach",
            "--task",
            "spsc",
            CLI_PROVIDER_FLAG,
            option.provider,
            MODEL_FLAG,
            option.model,
            *args,
        ),
    )


@given(st.text(min_size=1, max_size=12), st.text(min_size=1, max_size=24))
def test_a_harness_not_offered_is_refused_naming_the_vetted_models(
    tmp_path_factory: pytest.TempPathFactory, provider: str, model: str
) -> None:
    assume((provider, model) not in {(option.provider, option.model) for option in HARNESSES})
    project = _project(tmp_path_factory.mktemp("proj"))
    reply, launcher = _start(project, harness=HarnessChoice(provider=provider, model=model))
    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.UNVETTED_HARNESS
    for option in HARNESSES:
        if option.provider == provider:
            assert option.model in reply.message
    assert launcher.calls == []


def test_the_harness_is_checked_against_the_list_when_the_start_arrives(tmp_path: Path) -> None:
    project = _project(tmp_path / "proj")
    reply, launcher = _start(
        project, harness=HarnessChoice(provider="codex", model="gpt-a"), harnesses=()
    )
    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.UNVETTED_HARNESS
    assert "vetted models: none" in reply.message
    assert launcher.calls == []


# ----- arguments a helper start refuses -----


@given(
    _PLAIN_ARGS,
    _abbreviations(UNVETTED_MODEL_FLAG),
    _PLAIN_ARGS,
    st.one_of(st.none(), st.sampled_from(HARNESSES)),
)
def test_the_opt_out_is_refused_in_every_spelling_argparse_accepts(
    tmp_path_factory: pytest.TempPathFactory,
    before: list[str],
    opt_out: str,
    after: list[str],
    option: HarnessOption | None,
) -> None:
    project = _project(tmp_path_factory.mktemp("proj"))
    harness = (
        None if option is None else HarnessChoice(provider=option.provider, model=option.model)
    )
    reply, launcher = _start(project, args=(*before, opt_out, *after), harness=harness)
    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.INVALID_REQUEST
    assert opt_out in reply.message
    assert launcher.calls == []


@given(
    st.one_of(_abbreviations(CLI_PROVIDER_FLAG), _abbreviations(MODEL_FLAG)),
    st.sampled_from(HARNESSES),
)
def test_a_chosen_harness_is_the_only_source_of_provider_and_model(
    tmp_path_factory: pytest.TempPathFactory, flag: str, option: HarnessOption
) -> None:
    project = _project(tmp_path_factory.mktemp("proj"))
    choice = HarnessChoice(provider=option.provider, model=option.model)
    reply, launcher = _start(project, args=(flag, "x"), harness=choice)
    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.INVALID_REQUEST
    assert launcher.calls == []


def test_without_a_harness_the_project_may_still_name_its_model(tmp_path: Path) -> None:
    project = _project(tmp_path / "proj")
    reply, launcher = _start(project, args=(MODEL_FLAG, "gpt-a"))
    assert isinstance(reply, RunStartReply)
    assert launcher.calls == [
        (project.resolve(), ("--detach", "--task", "spsc", MODEL_FLAG, "gpt-a"))
    ]


@given(st.text(max_size=30))
def test_an_argument_that_does_not_abbreviate_the_opt_out_is_passed_on(
    tmp_path_factory: pytest.TempPathFactory, token: str
) -> None:
    name = token.split("=", 1)[0]
    assume(not (name.startswith("--") and len(name) > 2 and UNVETTED_MODEL_FLAG.startswith(name)))
    project = _project(tmp_path_factory.mktemp("proj"))
    reply, launcher = _start(project, args=(token,))
    assert isinstance(reply, RunStartReply)
    assert launcher.calls == [(project.resolve(), ("--detach", "--task", "spsc", token))]
