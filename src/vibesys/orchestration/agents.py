"""Agent creation and ``ctx.agents.turn``: rendering, isolation, retries, timeouts.

Split from ``runtime.py`` by capability; see that module's docstring.
"""

# Capabilities in this module share one private owner for resource lifetime.
# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import re
import subprocess
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from vibesys.agent_spec_config import agent_spec_from_config
from vibesys.context import _execution_status
from vibesys.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    CoreEventType,
    EventStatus,
    FrameworkSource,
    InvocationFinishedData,
    InvocationStartedData,
    PhaseData,
    json_value,
)
from vibesys.prompts import PROMPTS_DIR, render_template
from vibesys.run.run_control import splice_steering
from vibesys.runtime import (
    AgentDefinition,
    AgentHandle,
    CorrectionExhaustedError,
    Keyed,
    ReadOnly,
    Role,
    RoleIsolationError,
    WorkspaceScope,
)
from vibesys.schemas import SkillResourceSelection
from vibesys.skills import build_skill_catalog, resolve_skill_selections
from vs_agent.api import AgentSessionKey, SessionScope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator, Mapping
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack
    from typing import TextIO

    from vibesys.context import _RunResources
    from vibesys.orchestration.workspaces import WorkspaceHandle
    from vs_agent.api import (
        AgentCapabilities,
        AgentClientProtocol,
        AgentProgress,
        MCPServerSpec,
    )

T = TypeVar("T", bound=BaseModel)


def _unauthorized_paths(changes: list[str], allowed: tuple[str, ...]) -> list[str]:
    """Exclude the paths a read-only role may still update."""
    return [
        path
        for path in changes
        if not any(
            path == item.rstrip("/") or path.startswith(f"{item.rstrip('/')}/") for item in allowed
        )
    ]


def _split_template(template: str) -> tuple[Path, str]:
    """Split a ``Role.template`` path into ``render_template``'s ``(template_dir, name)``.

    Every template lives at ``prompts/loops/<strategy>/<name>`` or
    ``prompts/shared/<name>``; ``template_dir`` is always the strategy (or
    ``shared``) folder -- never a deeper directory -- so a role whose
    template itself lives in a subdirectory (e.g. a profiler's
    ``loops/multi/profilers/torch.j2``, which actually resolves from
    ``prompts/shared/profilers/torch.j2`` via the strategy folder's shared/
    fallback) still searches the same roots ``turns.py`` does today.
    """
    parts = Path(template).parts
    if len(parts) >= 3:  # noqa: PLR2004  # "loops"/"<strategy>"/<name...>
        return PROMPTS_DIR / parts[0] / parts[1], "/".join(parts[2:])
    return PROMPTS_DIR, template


def _context_kwargs(context: Mapping[str, object] | BaseModel) -> dict[str, object]:
    """Flatten a turn's prompt context into ``render_template`` kwargs."""
    if isinstance(context, BaseModel):
        return context.model_dump(mode="python")
    return dict(context)


def _filter_reply_skills(host: Any, reply: T) -> T:  # noqa: ANN401
    """Resolve every ``list[SkillResourceSelection]`` field on ``reply``.

    Mirrors the ``_skills`` helper every strategy's ``turns.py`` hand-rolls
    today: unknown skills and unsafe/missing resources are dropped (with a
    warning) rather than failing the turn.

    ``host`` is a ``vibesys.orchestration.runtime.RunContext``, typed ``Any``
    here (rather than imported under ``TYPE_CHECKING``) because that module
    imports this one for real to construct ``_Agents``; a back-reference,
    even type-checking-only, would be a tach module cycle (tach freezes
    ``TYPE_CHECKING`` imports too: ``ignore_type_checking_imports = false``).
    """
    updates: dict[str, list[SkillResourceSelection]] = {}
    for name, field_info in type(reply).model_fields.items():
        if field_info.annotation != list[SkillResourceSelection]:
            continue
        selections: list[SkillResourceSelection] = getattr(reply, name)
        if not selections:
            continue
        sources = host.environment.skill_source_paths
        if not sources:
            host.warning(
                "ignored skill recommendations because no skills are installed",
                source=FrameworkSource.LOOP,
                source_label="skills",
            )
            updates[name] = []
            continue
        try:
            resolved, diagnostics = resolve_skill_selections(
                selections, build_skill_catalog(sources)
            )
        except (OSError, ValueError) as error:
            host.warning(
                "ignored skill recommendations because the catalog is invalid",
                detail=f"{type(error).__name__}: {error}",
                source=FrameworkSource.LOOP,
                source_label="skills",
            )
            updates[name] = []
            continue
        for diagnostic in diagnostics:
            host.warning(diagnostic, source=FrameworkSource.LOOP, source_label="skills")
        updates[name] = [
            SkillResourceSelection(
                skill=item.skill,
                resource_paths=[
                    path.removeprefix(f"{item.skill}/") for path in item.resource_paths
                ],
                purpose=item.purpose,
            )
            for item in resolved
        ]
    return reply.model_copy(update=updates) if updates else reply


_active_progress: ContextVar[AgentProgress | None] = ContextVar(
    "vibesys_agent_progress", default=None
)


def _attempt_from_label(label: str) -> int | None:
    match = re.search(r"retry-(\d+)", label)
    return int(match.group(1)) if match else None


class _AgentClosedError(RuntimeError):
    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent {agent_id!r} is closed")


class _LocalAgentHandle:
    def __init__(  # noqa: PLR0913  # independently owned agent resources
        self,
        definition: AgentDefinition,
        context: _RunResources,
        client: AgentClientProtocol,
        resources: ExitStack,
        executor: ThreadPoolExecutor,
        scope_id: str | None,
        *,
        use_docker: bool,
    ) -> None:
        self._definition = definition
        self._resource_owner = context
        self._client = client
        self._resources = resources
        self._executor = executor
        self.scope_id = scope_id
        self._use_docker = use_docker
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return the features this handle's driver can enforce."""
        return self._client.capabilities

    @property
    def backend_name(self) -> str:
        """Return the selected agent backend."""
        return self._client.backend_name

    @property
    def driver_name(self) -> str | None:
        """Return the selected CLI driver name when one is configured."""
        return self._client.driver_name

    @property
    def provider(self) -> str | None:
        """Return the selected provider, when the backend has one."""
        return self._client.provider

    @property
    def model(self) -> str | None:
        """Return the model used for this handle's role."""
        return self._client.model_for_kind(self._definition.id)

    async def turn(self, message: str, *, system_prompt: str = "", label: str = "") -> str:
        """Run one text turn with run control and attributed lifecycle events."""
        if self._close_task is not None:
            raise _AgentClosedError(self._definition.id)
        kind = self._definition.id
        progress = _active_progress.get()

        def invoke(routed: str, execution_id: str) -> str:
            return self._client.invoke_text(
                kind=kind,
                workspace=self._resource_owner.workspace,
                system_prompt=system_prompt,
                user_prompt=routed,
                round_label=label,
                env=self._agent_env(),
                invocation_id=execution_id,
                session_key=AgentSessionKey(SessionScope.ROLE, kind),
                progress=progress,
            )

        return await asyncio.get_running_loop().run_in_executor(
            self._executor,
            partial(
                self._run_turn, message, system_prompt=system_prompt, label=label, invoke=invoke
            ),
        )

    async def turn_structured(  # noqa: PLR0913
        self,
        message: str,
        *,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        system_prompt: str = "",
        label: str = "",
        session_key: AgentSessionKey | None = None,
        reuse_session: bool | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
    ) -> T:
        """Run a typed turn through the same control and event path as text turns."""
        if self._close_task is not None:
            raise _AgentClosedError(self._definition.id)
        kind = self._definition.id
        progress = _active_progress.get()

        def invoke(routed: str, execution_id: str) -> T:
            return self._client.invoke(
                kind=kind,
                workspace=self._resource_owner.workspace,
                system_prompt=system_prompt,
                user_prompt=routed,
                response_cls=response_cls,
                fallback_factory=fallback_factory,
                round_label=label,
                env=self._agent_env(),
                invocation_id=execution_id,
                session_key=session_key or AgentSessionKey(SessionScope.ROLE, kind),
                reuse_session=reuse_session,
                mcp_servers=mcp_servers,
                progress=progress,
            )

        return await asyncio.get_running_loop().run_in_executor(
            self._executor,
            partial(
                self._run_turn, message, system_prompt=system_prompt, label=label, invoke=invoke
            ),
        )

    def _agent_env(self) -> dict[str, str]:
        context = self._resource_owner
        return {} if self._use_docker else context.device.gpu_env()

    def _run_turn[Result](
        self,
        message: str,
        *,
        system_prompt: str,
        label: str,
        invoke: Callable[[str, str], Result],
    ) -> Result:
        if self._closed:
            raise _AgentClosedError(self._definition.id)
        context = self._resource_owner
        self._client.set_log_file(context.run_log_file)
        control = context.integration.control
        control.raise_if_stopped()
        control.wait_while_paused()
        steering = control.take_pending_steer()
        message = splice_steering(message, steering)
        execution_id = uuid.uuid4().hex
        kind = self._definition.id
        attempt = _attempt_from_label(label)
        if steering:
            control.notify_steer_consumed(
                agent_kind=kind, round_label=label, execution_id=execution_id
            )
        fields = {"agent_kind": kind, "round_label": label, "execution_id": execution_id}
        events = context.events
        events.emit(
            CoreEventType.AGENT_EXECUTION_STARTED,
            status=EventStatus.ACTIVE,
            data=AgentExecutionStartedData(
                stage=kind,
                attempt=attempt,
                system_prompt=system_prompt,
                user_prompt=message,
                activity=AgentExecutionActivityData(mode="thinking", summary=f"{kind} is working"),
                driver=self._client.driver_name,
                provider=self._client.provider,
                model=self._client.model_for_kind(kind),
            ),
            **fields,
        )
        events.emit(
            CoreEventType.PHASE_STARTED,
            status=EventStatus.ACTIVE,
            data=PhaseData(phase=kind, attempt=attempt),
            **fields,
        )
        events.emit(
            CoreEventType.INVOCATION_STARTED,
            status=EventStatus.ACTIVE,
            data=InvocationStartedData(system_prompt=system_prompt, user_prompt=message),
            **fields,
        )
        result: Result | None = None
        error: BaseException | None = None
        try:
            result = invoke(message, execution_id)
        except BaseException as exc:
            error = exc
            raise
        finally:
            status = _execution_status(error)
            error_text = f"{type(error).__name__}: {error}" if error is not None else None
            events.emit(
                CoreEventType.AGENT_EXECUTION_FINISHED,
                status=status,
                data=AgentExecutionFinishedData(result=json_value(result), error=error_text),
                **fields,
            )
            events.emit(
                CoreEventType.INVOCATION_FINISHED,
                status=status,
                data=InvocationFinishedData(result=json_value(result), error=error_text),
                **fields,
            )
            events.emit(
                CoreEventType.PHASE_FINISHED,
                status=status,
                data=PhaseData(phase=kind, attempt=attempt),
                **fields,
            )
        return result

    def _close(self) -> None:
        """Close the client before its sandbox, including after failed turns."""
        if self._closed:
            return
        self._closed = True
        self._resources.close()

    def set_log_file(self, writer: TextIO) -> None:
        """Queue a log-writer update on the client's own worker thread."""
        if self._close_task is None:
            self._executor.submit(self._client.set_log_file, writer)

    async def close(self) -> None:
        """Close the client and sandbox on the agent's own worker thread."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        """Drain queued turns before releasing their client and sandbox."""
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._close)
        finally:
            await asyncio.to_thread(self._executor.shutdown, wait=True)


class _Agents:
    """Agent creation and per-turn progress for one run."""

    def __init__(self, host: Any) -> None:  # noqa: ANN401
        # See `_filter_reply_skills` above: `host` is `RunContext`, typed `Any`
        # to avoid a tach module cycle with `vibesys.orchestration.runtime`.
        self._host = host

    def default_definition(self, role_id: str, *, model: str | None = None) -> AgentDefinition:
        """Build a named role from this run's resolved agent configuration."""
        request = self._host.request
        spec = agent_spec_from_config(
            request.config,
            backend=request.agent_backend,
            provider=request.cli_provider,
            model=model,
        )
        return AgentDefinition(id=role_id, spec=spec)

    async def spawn(
        self, definition: AgentDefinition, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> AgentHandle:
        """Open a thread-affine client and sandbox for a named role."""
        return await self._host._spawn(definition, scope=self._host.workspaces._scope_of(scope))

    @contextmanager
    def progress(self, value: AgentProgress) -> Generator[None]:
        """Attribute turns spawned in the current async task."""
        token = _active_progress.set(value)
        try:
            yield
        finally:
            _active_progress.reset(token)

    async def turn(  # noqa: PLR0913
        self,
        role: Role,
        *,
        agent: AgentHandle,
        context: Mapping[str, object] | BaseModel,
        message: str | None = None,
        session_key: str | None = None,
        label: str,
        mcp_servers: list[MCPServerSpec] | None = None,
        correction_message: Callable[[BaseModel, str], str] | None = None,
        before_paid: Callable[[], Awaitable[None]] | None = None,
    ) -> BaseModel:
        """Run one role turn: render, isolate, retry, time out, all in one place.

        Owns every mechanic every strategy's ``turns.py`` hand-rolls today:
        prompt rendering from ``role.template`` (role's own folder, falling
        back to ``prompts/shared/``), a workspace snapshot before and after,
        reverting (and raising :class:`RoleIsolationError` if unrevertable)
        unauthorized edits from a :class:`~vibesys.runtime.ReadOnly` role,
        ``role.fallback()`` (or ``role.timeout_fallback(seconds)`` when the
        role declares one) on ``subprocess.TimeoutExpired`` (every role, not
        just today's multi/profile_multi implementer), up to
        ``role.max_corrections`` reprompts while ``role.check(reply)``
        returns an error, and skill-selection filtering when
        ``role.filter_skills``.

        ``before_paid`` is the strategy's one declared hook for paid-work
        bookkeeping (e.g. writing the progress-board's implementer-start
        marker) tied to ``role.paid``; it runs right before the pre-turn
        snapshot so a crash after the hook still resumes from a committed
        tree, matching today's paid-marker snapshot.
        """
        host = self._host
        workspace = host.workspaces.root
        template_dir, template_name = _split_template(role.template)
        prompt = render_template(
            template_name,
            template_dir=template_dir,
            **_context_kwargs(context),
        )
        user_message = role.message if message is None else message
        reuse_session = isinstance(role.session, Keyed)
        resolved_session_key = (
            AgentSessionKey(role.session.scope, session_key or role.id)
            if isinstance(role.session, Keyed)
            else None
        )

        if role.paid and before_paid is not None:
            await before_paid()
        revision = await workspace.snapshot(f"{label}-input")

        reply: BaseModel
        attempt = 0
        feedback: str | None = None
        try:
            while True:
                turn_label = label if attempt == 0 else f"{label}-retry-{attempt}"
                try:
                    reply = await agent.turn_structured(
                        feedback if feedback is not None else user_message,
                        system_prompt=prompt,
                        response_cls=role.reply,
                        fallback_factory=role.fallback,
                        label=turn_label,
                        session_key=resolved_session_key,
                        reuse_session=reuse_session,
                        mcp_servers=mcp_servers,
                    )
                except subprocess.TimeoutExpired as error:
                    host.log(
                        f"[{role.id}] attempt {attempt} timed out after {error.timeout:g} seconds"
                    )
                    reply = (
                        role.timeout_fallback(error.timeout)
                        if role.timeout_fallback is not None
                        else role.fallback()
                    )
                    break
                check_error = role.check(reply) if role.check is not None else None
                if check_error is None:
                    break
                if attempt >= role.max_corrections:
                    raise CorrectionExhaustedError(role.id)
                feedback = (
                    correction_message(reply, check_error)
                    if correction_message is not None
                    else f"Your previous response was invalid: {check_error}. "
                    "Correct it and return only the JSON object."
                )
                attempt += 1
        finally:
            if isinstance(role.access, ReadOnly):
                unauthorized = _unauthorized_paths(
                    await workspace.pending_changes(), role.access.allow
                )
                if unauthorized:
                    await workspace.restore(revision, clean=True, preserve_paths=role.access.allow)
                    remaining = _unauthorized_paths(
                        await workspace.pending_changes(), role.access.allow
                    )
                    if remaining:
                        raise RoleIsolationError(remaining, role=role.id)
                    host.log(
                        f"[role-isolation] reverted {len(unauthorized)} workspace change(s) "
                        f"attempted by {role.id}: {', '.join(unauthorized[:8])}"
                    )

        if role.filter_skills:
            reply = _filter_reply_skills(host, reply)
        await workspace.snapshot(label)
        return reply
