"""Detached chat process; reuses the legacy auxiliary agent, prompts and tools."""

from __future__ import annotations

import argparse
import os
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints import cli
from entrypoints.production import production_run_readers
from entrypoints.server import real_gateway_effects
from server.chat.prompts import experiment_chat_continuation_prompt, experiment_chat_system_prompt
from vibesys.api import AuxiliaryAgentLaunch, AuxiliaryReadableInput, AuxiliarySkills
from vibesys.api.chat import ChatRuntime, ChatSession, run_head
from vs_project.api import Project
from vs_proto.api import ChatMessageRequest, ChatSessionCreated
from vs_runlog.api import assert_run_unlocked
from vs_sim.api import OsThreads, ProcessTimeoutError, SystemClock

if TYPE_CHECKING:
    from vibesys.api import ManagedAgent, RunReaders

START_SECONDS = 10.0


def start_chat(request: ChatMessageRequest) -> None:
    """Start if needed, confirming ownership before releasing host admission."""
    directory = Project.chat_session_directory_for(
        request.project_root, request.run_id, request.session_id
    )
    log = directory / "session.log"
    try:
        assert_run_unlocked(log)
    except RuntimeError:
        return
    effects = real_gateway_effects()
    with (directory / "process.log").open("ab") as output:
        process = effects.spawn_python(
            "entrypoints.chat_session",
            [
                "--project",
                request.project_root,
                "--run",
                request.run_id,
                "--session",
                request.session_id,
            ],
            dict(os.environ),
            output,
        )
    deadline = effects.monotonic() + START_SECONDS
    while effects.monotonic() < deadline:
        try:
            assert_run_unlocked(log)
        except RuntimeError:
            return
        status = process.poll()
        if status is not None:
            if status == 0:
                return
            message = f"chat session exited {status}; see its process.log"
            raise ValueError(message)
        effects.sleep(0.05)
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except ProcessTimeoutError:
        process.kill()
        process.wait(timeout=5.0)
    message = "chat session did not acquire ownership within its startup deadline"
    raise ValueError(message)


def serve(request: ChatMessageRequest, runtime: ChatRuntime, readers: RunReaders) -> None:
    """Compose one independently owned conversation over a saved run's read boundary."""
    with ExitStack() as owned:
        session = ChatSession(request, runtime)
        owned.callback(session.close)
        run_request = cli.build_reader_request(Path(request.project_root), request.run_id)
        reader = readers(run_request)
        owned.callback(reader.close)
        ready = reader.ready()
        agents = reader.open_auxiliary_agents(sink=session.observe)
        owned.callback(agents.close)

        def open_agent() -> ManagedAgent:
            return agents.create_auxiliary_agent(
                AuxiliaryAgentLaunch(
                    role="chat",
                    member_id=request.session_id,
                    provider=ready.agent_provider,
                    model=ready.agent_model,
                    system_prompt=experiment_chat_system_prompt(),
                    continuation_prompt=experiment_chat_continuation_prompt(),
                    skills=AuxiliarySkills.CHAT,
                    readable_inputs=(
                        AuxiliaryReadableInput(
                            path=session.run_directory,
                            environment_variable="VIBESYS_RUN_FILES",
                            purpose="Read the run log, skeleton and blobs",
                        ),
                    ),
                )
            )

        session.serve(
            ChatSessionCreated(
                id=request.session_id, provider=ready.agent_provider, model=ready.agent_model
            ),
            open_agent,
        )


def serve_pending(request: ChatMessageRequest, runtime: ChatRuntime, readers: RunReaders) -> None:
    """Refresh an exiting owner when a new epoch still has admitted messages to answer."""
    while True:
        serve(request, runtime, readers)
        project = Project.open(request.project_root)
        if not project.state.session_inbox(request.run_id, request.session_id).pending():
            return
        _, parked = run_head(
            Project.run_log_path_for(request.project_root, request.run_id).parent / "run.head"
        )
        if parked:
            return


def main(argv: list[str] | None = None) -> int:
    """Open only the named session; a competing owner already serves its inbox."""
    parser = argparse.ArgumentParser(prog="vibesys chat-session")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--session", default="default")
    args = parser.parse_args(argv)
    request = ChatMessageRequest(
        project_root=str(args.project),
        run_id=args.run,
        session_id=args.session,
        request_id="process",
        text="process",
    )
    try:
        serve_pending(request, ChatRuntime(SystemClock(), OsThreads()), production_run_readers())
    except BlockingIOError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
