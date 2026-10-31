"""A question on a finished run starts or continues a chat; nothing of the run itself changes.

The read-only server of a finished run holds the chat conversation: a question or a new thread is
answered by an agent over the saved run, the conversation is appended to the run's journal and
nothing else is, and every command (a steer included) is refused with ``run_read_only``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.support import REQUEST_TIME, build_server_parts, run_record, sim_platform
from tests.support.chat_run import project_with_run, seed_agent_state
from tests.support.host_environment import unconfined_host_spec
from tests.support.real_git import NO_GIT
from tests.support.task_project import write_task

from launch import LaunchSettings, built_in_orchestrations, default_run_readers
from launch.agents import BuiltInSessionAgents
from server.api.protocol import (
    ChatQuery,
    ChatThreadCreateQuery,
    ControlCommand,
    PauseCommand,
    ResumeCommand,
    SnapshotQuery,
    SteerCommand,
    StopCommand,
)
from server.api.service import RunReadOnlyError
from server.events import READER_APPENDABLE_EVENTS, EventType
from server.host_api import RUN_EVENTS_FILE
from vibesys.api import (
    AuxiliaryAgentLaunch,
    PauseNowRequest,
    PauseRequest,
    ResumeRequest,
    RunReady,
    SteerRequest,
)
from vibesys.api.request import load_task
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import ResumeRef, RunRequest
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_evaluation.api.tool_relay import SOCKET_ENV, TOKEN_ENV, ToolRelayClient
from vs_project.api import OrchestrationDescriptor, OrchestrationRunManifest, Project
from vs_runtime.api.core import RunTiming
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    ManualClock,
    SimBlockingRunner,
    SimNetwork,
    SimThreads,
    examples,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.server.support import ServerParts

    from server.api.protocol import ProtocolRequest
    from vibesys.api import AuxiliaryAgents, CoreEvent, ManagedAgent, RunReader
    from vs_agent.api.testing import FakeInvocation

_SEEDS = settings(deadline=None, max_examples=examples(pr=8, nightly=200))


class _Agent:
    """A chat agent that answers every message with the run's own name for it."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.questions: list[str] = []

    def turn(self, message: str, *, invocation_id: str | None = None) -> str:
        del invocation_id
        self.questions.append(message)
        return f"{self.label}: {message}"

    def close(self) -> None:
        """Nothing to release."""


class _Agents:
    """The conversations a reader opens: one `_Agent` each."""

    def __init__(self) -> None:
        self.agents: list[_Agent] = []

    def create_auxiliary_agent(self, launch: AuxiliaryAgentLaunch) -> ManagedAgent:
        agent = _Agent(launch.member_id)
        self.agents.append(agent)
        return agent

    def close(self) -> None:
        """Nothing to release."""


class _Reader:
    """The reader of the saved run: it announces the run and opens conversations over it."""

    def __init__(self, ready: RunReady, agents: _Agents) -> None:
        self._ready = ready
        self.agents = agents
        self.closed = False

    def ready(self) -> RunReady:
        return self._ready

    def open_auxiliary_agents(
        self, *, sink: Callable[[CoreEvent], object] | None = None
    ) -> AuxiliaryAgents:
        del sink
        return self.agents

    def close(self) -> None:
        self.closed = True


class _EndedRun:
    """A finished run: its saved project, and the journal it ended with."""

    def __init__(self, root: Path) -> None:
        (root / "project").mkdir()
        write_task(
            root / "project",
            'version = 1\n[agent]\ndomain = "generic"\n'
            '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        )
        self.project, self.run_id = project_with_run(root / "project")
        seed_agent_state(self.project, self.run_id)
        self.journal = root / "journal"
        finished = build_server_parts(threads=SimThreads())
        finished.journal.attach(self.journal, run_id=self.run_id)
        finished.controller.finish()
        finished.close()
        self.root = root

    def ready(self) -> RunReady:
        state = self.root / "state"
        return RunReady(
            record=run_record(self.project, self.run_id),
            project_root=self.project.root,
            log_directory=self.journal,
            frontend_state_directory=state,
            agent_provider="codex",
            agent_model="gpt-test",
            agent_providers=("codex",),
        )

    def lines(self) -> list[str]:
        return (self.journal / RUN_EVENTS_FILE).read_text().splitlines()


@pytest.fixture(scope="module")
def ended_run(tmp_path_factory: pytest.TempPathFactory) -> _EndedRun:
    return _EndedRun(tmp_path_factory.mktemp("ended"))


def _serve(run: _EndedRun, copy: Path, threads: SimThreads) -> tuple[ServerParts, _Reader]:
    """A read-only server over a copy of the run's journal, with chat attached through a reader."""
    shutil.copytree(run.journal, copy)
    parts = build_server_parts(threads=threads)
    parts.controller.attach_read_only(copy, run_id=run.run_id)
    reader = _Reader(run.ready(), _Agents())
    parts.integration.attach_reader(reader)
    return parts, reader


def test_a_question_on_an_ended_run_gets_an_answer_from_a_chat_agent(
    tmp_path: Path, ended_run: _EndedRun
) -> None:
    threads = SimThreads(schedule_seed=0)

    def main() -> tuple[str, list[str]]:
        parts, reader = _serve(ended_run, tmp_path / "j", threads)
        response = parts.api.execute(ChatQuery(timestamp=REQUEST_TIME, text="what worked?"))
        assert response.chat is not None
        return response.chat.answer, [q for agent in reader.agents.agents for q in agent.questions]

    answer, asked = threads.run(main)

    assert answer.endswith("what worked?")
    assert "not available" not in answer
    assert asked == ["what worked?"]


_TEXT = st.text(min_size=1, max_size=30).filter(lambda text: text.strip() != "")
_CONTROL_ID = st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True)


def _commands(text: str, control_id: str) -> list[ProtocolRequest]:
    """One of every kind of request that would change a run, as the wire carries it."""
    return [
        PauseCommand(timestamp=REQUEST_TIME),
        ResumeCommand(timestamp=REQUEST_TIME),
        StopCommand(timestamp=REQUEST_TIME),
        SteerCommand(timestamp=REQUEST_TIME, text=text),
        ControlCommand(
            timestamp=REQUEST_TIME, control=SteerRequest(control_id=control_id, text=text)
        ),
        ControlCommand(timestamp=REQUEST_TIME, control=PauseRequest(control_id=control_id)),
        ControlCommand(timestamp=REQUEST_TIME, control=ResumeRequest(control_id=control_id)),
        ControlCommand(timestamp=REQUEST_TIME, control=PauseNowRequest(control_id=control_id)),
    ]


@_SEEDS
@given(
    seed=st.integers(0, 2**32),
    text=_TEXT,
    control_id=_CONTROL_ID,
    script=st.lists(
        st.sampled_from(["command", "question", "thread", "thread_question"]),
        min_size=1,
        max_size=8,
    ),
    which=st.integers(0, 7),
)
def test_nothing_but_the_conversation_is_added_to_a_finished_runs_journal(  # noqa: PLR0913  # lint-waiver: LW-101230 [PLR0913]; a Hypothesis test takes each generated input as one argument
    tmp_path_factory: pytest.TempPathFactory,
    ended_run: _EndedRun,
    seed: int,
    text: str,
    control_id: str,
    script: list[str],
    which: int,
) -> None:
    """Whatever mix of commands, questions and threads comes, the run's own record is untouched.

    Every command is refused as ``run_read_only``; every question and thread adds only chat
    events; the events the run ended with are, in order and byte for byte, still the journal's
    beginning.
    """
    threads = SimThreads(schedule_seed=seed)
    copy = tmp_path_factory.mktemp("copy") / "j"
    before = ended_run.lines()

    def main() -> tuple[list[str], list[str]]:
        parts, _reader = _serve(ended_run, copy, threads)
        refused: list[str] = []
        thread_ids: list[str] = []
        for step in script:
            match step:
                case "command":
                    request = _commands(text, control_id)[which]
                    with pytest.raises(RunReadOnlyError) as refusal:
                        parts.api.execute(request)
                    refused.append(refusal.value.diagnostic_code)
                case "question":
                    parts.api.execute(ChatQuery(timestamp=REQUEST_TIME, text=text))
                case "thread":
                    created = parts.api.execute(ChatThreadCreateQuery(timestamp=REQUEST_TIME))
                    assert created.chat_thread is not None
                    thread_ids.append(created.chat_thread.thread_id)
                case "thread_question":
                    if thread_ids:
                        parts.api.execute(
                            ChatQuery(timestamp=REQUEST_TIME, text=text, thread_id=thread_ids[-1])
                        )
        return refused, thread_ids

    refused, _threads = threads.run(main)

    after = (copy / RUN_EVENTS_FILE).read_text().splitlines()
    assert after[: len(before)] == before
    added = {json.loads(line)["type"] for line in after[len(before) :]}
    assert added <= {event.value for event in READER_APPENDABLE_EVENTS}
    assert set(refused) <= {"run_read_only"}


def test_a_steer_on_an_ended_run_is_refused_and_reaches_no_one(
    tmp_path: Path, ended_run: _EndedRun
) -> None:
    threads = SimThreads(schedule_seed=0)

    def main() -> str:
        parts, reader = _serve(ended_run, tmp_path / "j", threads)
        with pytest.raises(RunReadOnlyError) as refusal:
            parts.api.execute(SteerCommand(timestamp=REQUEST_TIME, text="try the other idea"))
        assert [q for agent in reader.agents.agents for q in agent.questions] == []
        return refusal.value.diagnostic_code

    assert threads.run(main) == "run_read_only"
    assert (tmp_path / "j" / RUN_EVENTS_FILE).read_text().splitlines() == ended_run.lines()


def test_a_conversation_on_an_ended_run_is_there_when_the_run_is_opened_again(
    tmp_path: Path, ended_run: _EndedRun
) -> None:
    threads = SimThreads(schedule_seed=0)

    def first() -> str:
        parts, _reader = _serve(ended_run, tmp_path / "j", threads)
        created = parts.api.execute(ChatThreadCreateQuery(timestamp=REQUEST_TIME))
        assert created.chat_thread is not None
        thread_id = created.chat_thread.thread_id
        parts.api.execute(
            ChatQuery(timestamp=REQUEST_TIME, text="which idea won?", thread_id=thread_id)
        )
        return thread_id

    thread_id = threads.run(first)

    reopened = SimThreads(schedule_seed=0)

    def second() -> tuple[list[str], str | None, list[str]]:
        parts = build_server_parts(threads=reopened)
        parts.controller.attach_read_only(tmp_path / "j", run_id=ended_run.run_id)
        snapshot = parts.api.execute(SnapshotQuery(timestamp=REQUEST_TIME)).snapshot
        assert snapshot is not None
        return (
            [thread.thread_id for thread in snapshot.chat_threads],
            snapshot.last_chat_thread_id,
            [event.text for event in parts.journal.read_history() if event.type is EventType.CHAT],
        )

    threads_listed, last, questions = reopened.run(second)

    assert threads_listed == [thread_id]
    assert last == thread_id
    assert questions == ["which idea won?"]


def _saved_state(run: _EndedRun) -> dict[str, bytes]:
    """The run's project and its machine-local logs, file by file."""
    logs = Project.log_directory_for(run.project.root, run.run_id)
    return {
        str(path): path.read_bytes()
        for root in (run.project.root, logs)
        if root.exists()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class _RunTools:
    """What the chat agent's answer is built from: the run tools, called through the relay."""

    def __init__(self, threads: SimThreads, network: SimNetwork) -> None:
        self._threads = threads
        self._network = network
        self.tools_called: list[str] = []

    def answer(self, invocation: FakeInvocation) -> str:
        """Read the saved run through the tool server this conversation was handed."""
        [server] = invocation.tool_servers or []
        env = dict(server.runtime_env)
        client = ToolRelayClient(
            Path(env[SOCKET_ENV]), env[TOKEN_ENV], network=self._network, clock=self._threads
        )
        self.tools_called.append("run_summary")
        return "The run says: " + client.call("run_summary", {}).text


def _reader_of(run: _EndedRun, threads: SimThreads, tools: _RunTools) -> RunReader:
    """The production reader of the saved run, over Fakes of the agent, the backend and the clock."""
    network = tools._network  # noqa: SLF001  # lint-waiver: LW-101231 [SLF001]; the test owns both objects
    client = FakeAgentClient(
        threads=threads, capabilities=AgentCapabilities(tool_servers=True, session_reuse=True)
    )
    client.set_text(None, tools.answer)
    settings = LaunchSettings(
        timing=RunTiming(ManualClock(), threads),
        processes=sim_platform(threads).processes,
        piped_processes=FakePipedProcessLauncher(),
        blocking=SimBlockingRunner(threads),
        network=network,
        backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(threads=threads),
        agent_client_factory=lambda **_services: client,
        agents=BuiltInSessionAgents(
            client_factory=lambda **_services: client,
            network=network,
            git=NO_GIT,
            registry=built_in_orchestrations(),
        ),
    )
    manifest = run.project.state.load_run(run.run_id)
    assert isinstance(manifest, OrchestrationRunManifest)
    request = RunRequest(
        project_root=run.project.root,
        orchestration=OrchestrationDescriptor(
            id=manifest.orchestration.id,
            config_version=manifest.orchestration.config_version,
            options=manifest.orchestration.options,
        ),
        config=Config.model_validate(
            {"model": {"name": "gpt-6-luna"}, "agent": {"backend": "cli", "cli_provider": "codex"}}
        ),
        input_bundle=load_task(run.project.root),
        resume=ResumeRef(run_id=run.run_id),
        run_environment=unconfined_host_spec(),
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        agent_backend="cli",
        cli_provider="codex",
    )
    return default_run_readers(settings)(request)


def test_a_question_on_an_ended_run_is_answered_from_the_runs_own_tools(
    tmp_path: Path, ended_run: _EndedRun
) -> None:
    """The reader of a saved run serves its tools to a real chat conversation, as a live run does."""
    threads = SimThreads(schedule_seed=0)
    network = SimNetwork(threads)
    tools = _RunTools(threads, network)
    saved = _saved_state(ended_run)

    def main() -> str:
        shutil.copytree(ended_run.journal, tmp_path / "j")
        parts = build_server_parts(threads=threads)
        parts.controller.attach_read_only(tmp_path / "j", run_id=ended_run.run_id)
        reader = _reader_of(ended_run, threads, tools)
        parts.integration.attach_reader(reader)
        try:
            response = parts.api.execute(
                ChatQuery(timestamp=REQUEST_TIME, text="what did it find?")
            )
            assert response.chat is not None
            return response.chat.answer
        finally:
            parts.close()

    answer = threads.run(main)

    assert tools.tools_called == ["run_summary"]
    assert "idea_count: 1" in answer
    # The run's saved state, apart from its journal's conversation, is as it was.
    assert _saved_state(ended_run) == saved
