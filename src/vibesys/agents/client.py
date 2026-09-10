"""VibeSys agent service shared by all external-agent drivers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from vibesys.agent_runner import (
    log_and_print,
    log_json_and_print,
    log_markdown_and_print,
    log_prompt_markdown_and_print,
    parse_typed_response_text,
)
from vibesys.agents.cli_common import agent_label, materialize_skills
from vibesys.agents.contracts import (
    AgentCapabilities,
    AgentDriver,
    AgentEvent,
    AgentEventKind,
    AgentExecutionPolicy,
    AgentObserver,
    AgentSession,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    AgentUsage,
    MCPServerSpec,
    SessionDisposition,
    session_spec_fingerprint,
)
from vibesys.agents.session_key import AgentSessionKey, SessionScope
from vibesys.agents.session_store import NullSessionStore, SessionStore
from vibesys.run.events import CommandResultPayload, JsonResultPayload

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path
    from typing import TextIO

    from langchain_core.tools import BaseTool

    from vibesys.agents.callbacks import AgentLogger
    from vibesys.agents.progress import AgentProgress
    from vibesys.constants import ComputeBackend
    from vs_sandbox import HostResource, ProjectPathPolicy

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class _CachedSession:
    spec: AgentSessionSpec
    session: AgentSession
    #: The provider conversation the last completed turn on this session ran
    #: in. Kept beside the live handle so a caller can name the conversation
    #: its next turn continues without a durable store being wired.
    provider_session_id: str | None = None


class AgentDiagnosticLog:
    """Mutable diagnostic destination shared by a client and its driver."""

    def __init__(self, stream: TextIO | None) -> None:
        """Create a log target backed by ``stream``."""
        self.stream = stream

    def __call__(self, message: str) -> None:
        """Write one driver diagnostic to the current run log."""
        log_and_print(message, self.stream)


class _LoggerObserver:
    """Translate neutral driver events into VibeSys's application logger."""

    def __init__(self, logger: AgentLogger) -> None:
        self._logger = logger

    def on_event(self, event: AgentEvent) -> None:
        """Render one normalized driver event."""
        if event.kind is AgentEventKind.TEXT:
            self._logger.log_text(event.text or "")
            return

        self._logger.end_text()
        if event.kind is AgentEventKind.THINKING:
            self._logger.on_thinking(event.text or "")
        elif event.kind is AgentEventKind.TOOL_CALL:
            tool = str(event.payload.get("tool", "unknown"))
            args = event.payload.get("args")
            self._logger.on_tool_call(tool, args if isinstance(args, (dict, str)) else None)
        elif event.kind is AgentEventKind.TOOL_RESULT:
            exit_code = event.payload.get("exit_code")
            duration = event.payload.get("duration")
            result_payload = event.payload.get("result_payload")
            self._logger.on_tool_result(
                str(event.payload.get("tool", "unknown")),
                stdout=str(event.payload.get("stdout", event.text or "")),
                stderr=str(event.payload.get("stderr", "")),
                exit_code=exit_code if isinstance(exit_code, int) else None,
                duration=float(duration) if isinstance(duration, (int, float)) else None,
                payload=(
                    result_payload
                    if isinstance(result_payload, (CommandResultPayload, JsonResultPayload))
                    else None
                ),
            )
        elif event.kind is AgentEventKind.USAGE and event.usage is not None:
            self._logger.update_usage(_usage_dict(event.usage))

    def close(self) -> None:
        """Close any assistant-text segment left open at turn completion."""
        self._logger.end_text()


def _usage_dict(usage: AgentUsage) -> dict[str, int | float | None]:
    return {
        "input_tokens": usage.input_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "output_tokens": usage.output_tokens,
        "total_cost_usd": usage.total_cost_usd,
        "duration_ms": usage.duration_ms,
    }


def _normalize_mcp_servers(
    servers: Iterable[MCPServerSpec] | None,
) -> tuple[MCPServerSpec, ...]:
    """Freeze the caller's server list so it can key a session spec."""
    if servers is None:
        return ()
    return tuple(servers)


class AgentClient:
    """Run agent turns and own the resulting configured sessions.

    A non-``None`` session key resumes a conversation only while its immutable
    session specification remains equal. Changing setup configuration closes
    and replaces the old session. An unkeyed turn uses an ephemeral session.
    """

    backend_name = "cli"

    def __init__(  # noqa: PLR0913
        self,
        driver: AgentDriver,
        *,
        provider: str | None = None,
        skills: Iterable[Path] = (),
        compute_backend: ComputeBackend | None = None,
        model_name: str | None = None,
        timeout: int | None = None,
        run_log_file: TextIO | None = None,
        log_dir: Path | None = None,
        default_reasoning_effort: str | None = None,
        role_models: Mapping[str, str] | None = None,
        role_reasoning_efforts: Mapping[str, str] | None = None,
        project_path_policy: ProjectPathPolicy | None = None,
        host_resources: Iterable[HostResource] = (),
        require_host_sandbox: bool = False,
        containerized: bool = False,
        driver_log: AgentDiagnosticLog | None = None,
        driver_name: str | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        """Create a client that owns ``driver`` and every session it creates."""
        self._driver = driver
        self._driver_name = driver_name
        self._session_store: SessionStore = session_store or NullSessionStore()
        self._provider = provider
        self._skills = tuple(skills)
        self._compute_backend = compute_backend
        self._model_name = model_name
        self._timeout = timeout
        self._run_log_file = run_log_file
        self._driver_log = driver_log
        self._log_dir = log_dir
        self._default_reasoning_effort = default_reasoning_effort
        self._role_models = dict(role_models or {})
        self._role_reasoning_efforts = dict(role_reasoning_efforts or {})
        self._policy = AgentExecutionPolicy(
            project_paths=project_path_policy,
            host_resources=tuple(host_resources),
            require_enforcement=require_host_sandbox,
            containerized=containerized,
        )
        self._sessions: dict[AgentSessionKey, _CachedSession] = {}
        # Where each key's last completed turn ran, kept across evictions so a
        # caller can tell a retired conversation from a replaced one.
        self._last_turn_sessions: dict[AgentSessionKey, str | None] = {}
        self._closed = False

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return the selected driver's factual capabilities."""
        return self._driver.capabilities

    @property
    def driver_name(self) -> str | None:
        """Return the stable configured driver name (``"agentshim"``/``"omnigent"``).

        This is the application-configuration string, not the driver's Python
        class name, so it stays stable across implementation refactors.
        """
        return self._driver_name

    @property
    def provider(self) -> str | None:
        """Return the configured CLI provider (``"codex"``, ``"claude"``, ...)."""
        return self._provider or "codex"

    def model_for_kind(self, kind: str) -> str | None:
        """Return the effective model for ``kind``, honoring role overrides."""
        return self._role_models.get(kind, self._model_name)

    def set_log_file(self, stream: TextIO | None) -> None:
        """Direct subsequent application logs to ``stream``."""
        self._run_log_file = stream
        if self._driver_log is not None:
            self._driver_log.stream = stream

    def invoke(  # noqa: PLR0913
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str,
        env: dict[str, str] | None = None,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        tools: list[BaseTool] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> T:
        """Run one turn and parse its structured response."""
        del tools  # In-process tools remain a deepagents-only compatibility path.
        result = self._invoke_turn(
            kind=kind,
            workspace=workspace,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_schema=response_cls,
            round_label=round_label,
            env=env,
            invocation_id=invocation_id,
            progress=progress,
            mcp_servers=mcp_servers,
            reuse_session=reuse_session,
            session_key=session_key,
        )
        label = agent_label(kind)
        parsed = parse_typed_response_text(result.text, response_cls)
        if parsed is None:
            log_and_print(f"\n=== {label} ROUND OUTPUT (missing response) ===", self._run_log_file)
            log_and_print(
                f"No structured response received from {label.lower()}.",
                self._run_log_file,
            )
            if result.text:
                log_and_print(
                    f"\n=== {label} ROUND OUTPUT (raw output) ===",
                    self._run_log_file,
                )
                log_markdown_and_print(result.text, self._run_log_file)
            return fallback_factory()
        log_and_print(f"\n=== {label} ROUND OUTPUT ===", self._run_log_file)
        log_json_and_print(parsed.model_dump_json(indent=2), self._run_log_file)
        return parsed

    def invoke_text(  # noqa: PLR0913
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        round_label: str,
        env: dict[str, str] | None = None,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        tools: list[BaseTool] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> str:
        """Run one conversational turn without a structured-output requirement."""
        del tools
        result = self._invoke_turn(
            kind=kind,
            workspace=workspace,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_schema=None,
            round_label=round_label,
            env=env,
            invocation_id=invocation_id,
            progress=progress,
            mcp_servers=mcp_servers,
            reuse_session=reuse_session,
            session_key=session_key,
        )
        label = agent_label(kind)
        if result.text:
            log_and_print(f"\n=== {label} ROUND OUTPUT ===", self._run_log_file)
            log_markdown_and_print(result.text, self._run_log_file)
        else:
            log_and_print(f"\n=== {label} ROUND OUTPUT (missing response) ===", self._run_log_file)
            log_and_print(f"No response received from {label.lower()}.", self._run_log_file)
        return result.text

    def _invoke_turn(  # noqa: PLR0913
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        output_schema: type[BaseModel] | None,
        round_label: str,
        env: dict[str, str] | None,
        invocation_id: str | None,
        progress: AgentProgress | None,
        mcp_servers: list[MCPServerSpec] | None,
        reuse_session: bool | None,
        session_key: AgentSessionKey | None,
    ) -> AgentTurnResult:
        label = agent_label(kind)
        model = self._role_models.get(kind, self._model_name)
        reasoning_effort = self._role_reasoning_efforts.get(kind, self._default_reasoning_effort)
        spec = AgentSessionSpec(
            role=kind,
            provider=self._provider or "codex",
            workspace=workspace,
            policy=self._policy,
            model=model,
            mcp_servers=_normalize_mcp_servers(mcp_servers),
            skills=self._skills,
            environment=tuple(sorted((env or {}).items())),
            reasoning_effort=reasoning_effort,
        )
        turn = AgentTurnRequest(
            message=user_prompt,
            instructions=system_prompt,
            output_schema=output_schema,
            timeout=timedelta(seconds=self._timeout) if self._timeout is not None else None,
            invocation_id=invocation_id,
            label=round_label,
        )
        from vibesys.agents.callbacks import AgentLogger  # noqa: PLC0415

        logger = AgentLogger(
            log_file=self._run_log_file,
            model_name=model,
            agent_label=label,
            progress=progress,
            agent_kind=kind,
            round_label=round_label,
            invocation_id=invocation_id,
        )
        log_and_print(f"\n=== {label} ROUND START: {round_label} ===", self._run_log_file)
        log_and_print(
            f"driver: {self.driver_name or type(self._driver).__name__}, provider: {spec.provider}, "
            f"model: {model}, reasoning_effort: {reasoning_effort or 'provider_default'}, "
            f"cwd: {workspace}",
            self._run_log_file,
        )
        log_and_print("--- input ---", self._run_log_file)
        log_prompt_markdown_and_print(f"{system_prompt}\n\n{user_prompt}", self._run_log_file)
        reuse = reuse_session if reuse_session is not None else True
        # An unscoped call still needs one live conversation per role, but a
        # bare role is not a conversation a later process could identify, so the
        # fallback key deliberately lands in a scope that is never checkpointed.
        cache_key = session_key or AgentSessionKey(SessionScope.ROLE, kind)
        result: AgentTurnResult | None = None
        observer = _LoggerObserver(logger)
        try:
            result = self.run(
                session_spec=spec,
                turn=turn,
                session_key=cache_key if reuse else None,
                observer=observer,
            )
        except Exception as exc:
            observer.close()
            log_and_print(f"\n=== {label} ROUND ERROR: {round_label} ===", self._run_log_file)
            log_and_print(f"{type(exc).__name__}: {exc}", self._run_log_file)
            raise
        finally:
            observer.close()
            # The result may not exist after a setup/turn failure. The empty
            # record preserves one audit row per attempted invocation.
            self._write_usage_record(
                kind=kind,
                round_label=round_label,
                model=model,
                reasoning_effort=reasoning_effort,
                usage=result.usage if result is not None else AgentUsage(),
            )
        assert result is not None  # noqa: S101  # assigned or the exception propagated
        return result

    def _write_usage_record(
        self,
        *,
        kind: str,
        round_label: str,
        model: str | None,
        reasoning_effort: str | None,
        usage: AgentUsage,
    ) -> None:
        if self._log_dir is None:
            return
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": kind,
            "round_label": round_label,
            "provider": self._provider,
            "model": model,
            "reasoning_effort": reasoning_effort,
            **_usage_dict(usage),
        }
        target = self._log_dir / "usage.jsonl"
        try:
            with target.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
        except OSError as exc:
            log_and_print(
                f"[usage] failed to append {target}: {type(exc).__name__}: {exc}",
                self._run_log_file,
            )

    def run(
        self,
        *,
        session_spec: AgentSessionSpec,
        turn: AgentTurnRequest,
        session_key: AgentSessionKey | None = None,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Run one raw turn, optionally retaining its session for reuse."""
        self._ensure_open()
        if session_key is None:
            return self._run_ephemeral(session_spec, turn, observer)

        fingerprint = session_spec_fingerprint(session_spec)
        cached = self._sessions.get(session_key)
        if cached is not None and cached.spec != session_spec:
            # The configuration changed within this process. Drop the live
            # session; the checkpoint is left alone because the fingerprint
            # check below already refuses it, and this turn overwrites it.
            self._evict(session_key)
            cached = None
        if cached is None:
            session = self._create_session(session_spec)
            # A fresh session in a resumed process holds no conversation. Offer
            # it the checkpointed provider ID so its very first turn resumes
            # (``codex exec resume`` / ``claude --resume``) instead of replaying
            # the round from scratch.
            self._resume_checkpoint(session, session_key, fingerprint)
            cached = _CachedSession(spec=session_spec, session=session)
            self._sessions[session_key] = cached

        try:
            result = cached.session.run_turn(turn, observer)
        except BaseException as error:
            # The checkpoint is deliberately kept. A turn can fail for reasons
            # that say nothing about the conversation's validity (a timeout, a
            # cancelled run), and a driver that finds its conversation
            # unusable reports RESET_REQUIRED instead of raising.
            try:
                self._evict(session_key)
            except Exception as cleanup_error:  # noqa: BLE001  # preserve the turn failure
                error.add_note(f"agent session cleanup also failed: {cleanup_error}")
            raise

        # Recorded for both dispositions, and exactly as reported: this is where
        # the turn ran, which a reset afterwards does not change.
        self._last_turn_sessions[session_key] = result.provider_session_id
        if result.disposition is SessionDisposition.RESET_REQUIRED:
            # The driver restarted or abandoned the conversation this key names,
            # so both the live session and the checkpoint describe history that
            # no longer exists.
            self._evict(session_key)
            self._session_store.clear(session_key)
        else:
            if result.provider_session_id is not None:
                cached.provider_session_id = result.provider_session_id
            self._checkpoint(session_key, session_spec, fingerprint, result)
        return result

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Name the provider conversation the next turn on ``session_key`` continues.

        ``None`` means the next turn starts from no history at all: no live
        session holds a conversation, and no checkpoint survives for the key.
        A caller that shortens a prompt because a conversation already carries
        its instructions asks this first.
        """
        cached = self._sessions.get(session_key)
        if cached is not None and cached.provider_session_id is not None:
            return cached.provider_session_id
        record = self._session_store.get(session_key)
        return None if record is None else record.session_id

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Name the provider conversation the last completed turn on the key ran in.

        A driver-reported restart does not clear this, which is what separates
        it from :meth:`provider_session_id`: a conversation retired *after* it
        served a turn still answered that turn, while one replaced *while*
        serving it did not. A caller that shortened a prompt compares this
        against the conversation that justified the shortening; a difference
        means the shortened prompt reached an agent without its instructions.
        """
        return self._last_turn_sessions.get(session_key)

    def _resume_checkpoint(
        self,
        session: AgentSession,
        session_key: AgentSessionKey,
        fingerprint: str,
    ) -> None:
        """Offer a freshly created session the checkpoint stored for its key."""
        record = self._session_store.get(session_key)
        if record is None:
            return
        if record.spec_fingerprint != fingerprint:
            # Configuration drifted since the checkpoint was written. Refusing
            # it is the same rule that evicts a live session whose spec no
            # longer matches; this turn replaces the entry.
            self._session_store.clear(session_key)
            return
        if not session.resume_provider_session(record.session_id):
            # The driver refused the ID, so nothing will ever resume it: the
            # provider cannot resume at all, or the session already holds a
            # newer conversation. Either way the checkpoint is dead.
            self._session_store.clear(session_key)

    def _checkpoint(
        self,
        session_key: AgentSessionKey,
        spec: AgentSessionSpec,
        fingerprint: str,
        result: AgentTurnResult,
    ) -> None:
        """Checkpoint the provider session ID a completed turn reported."""
        if result.provider_session_id is None:
            # No resumable ID this turn (e.g. a driver without provider session
            # support); keep any prior ID rather than clobbering it with None.
            return
        self._session_store.record(
            session_key,
            spec_fingerprint=fingerprint,
            provider=spec.provider,
            model=spec.model,
            session_id=result.provider_session_id,
            role=spec.role,
        )

    def close(self) -> None:
        """Close all cached sessions and the driver exactly once."""
        if self._closed:
            return
        self._closed = True
        first_error: Exception | None = None
        for key in tuple(self._sessions):
            try:
                self._evict(key)
            except Exception as error:  # noqa: BLE001  # cleanup must continue
                if first_error is None:
                    first_error = error
        try:
            self._driver.close()
        except Exception as error:  # noqa: BLE001  # preserve earlier cleanup failures
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error

    def _run_ephemeral(
        self,
        session_spec: AgentSessionSpec,
        turn: AgentTurnRequest,
        observer: AgentObserver | None,
    ) -> AgentTurnResult:
        session = self._create_session(session_spec)
        turn_error: BaseException | None = None
        try:
            return session.run_turn(turn, observer)
        except BaseException as error:
            turn_error = error
            raise
        finally:
            try:
                session.close()
            except Exception as cleanup_error:
                if turn_error is None:
                    raise
                turn_error.add_note(f"agent session cleanup also failed: {cleanup_error}")

    def _evict(self, key: AgentSessionKey) -> None:
        cached = self._sessions.pop(key, None)
        if cached is not None:
            cached.session.close()

    def _create_session(self, spec: AgentSessionSpec) -> AgentSession:
        """Perform VibeSys-owned setup, then delegate runtime-specific setup."""
        materialize_skills(
            spec.workspace,
            list(spec.skills),
            compute_backend=self._compute_backend,
            log_file=self._run_log_file,
        )
        return self._driver.create_session(spec)

    def _ensure_open(self) -> None:
        if self._closed:
            msg = "agent client is closed"
            raise RuntimeError(msg)

    def __enter__(self) -> AgentClient:
        """Return this open client as a context manager."""
        self._ensure_open()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Close the client when leaving its context."""
        self.close()
