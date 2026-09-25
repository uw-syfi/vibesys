"""Unit tests for ``ctx.agents.turn`` (``vibesys.orchestration.runtime._Agents.turn``).

Exercises the mechanics ``Role`` declares against a synthetic fixture
template (rendering, isolation revert/raise, timeout fallback, correction
retries + exhaustion, skill filtering, session-key reuse), plus a Hypothesis
property test over random behavior sequences. A separate differential test
(``test_agents_turn_differential.py``) proves ``ctx.agents.turn`` renders the
exact same prompt text today's strategy ``turns.py`` wrappers do.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel
from tests.vibesys.orchestration.harness import run_with_context

from vibesys.runtime import (
    CorrectionExhaustedError,
    Fresh,
    Keyed,
    ReadOnly,
    Role,
    RoleIsolationError,
    Writes,
)
from vibesys.schemas import SkillResourceSelection
from vs_agent.api import SessionScope
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext


class _Reply(BaseModel):
    text: str = "ok"


class _TestContext(BaseModel):
    subject: str


class _SkillReply(BaseModel):
    text: str = "ok"
    recommended_skills: list[SkillResourceSelection] = []


def _fallback() -> _Reply:
    return _Reply(text="fallback")


def _write_fixture_templates(root: Path) -> None:
    testrole = root / "loops" / "testrole"
    testrole.mkdir(parents=True, exist_ok=True)
    (testrole / "prompt.j2").write_text("Role prompt for {{ subject }}.\n")
    (root / "shared").mkdir(parents=True, exist_ok=True)


def _role(  # noqa: PLR0913  # test helper mirroring every Role field
    *,
    reply: type[BaseModel] = _Reply,
    fallback: Callable[[], BaseModel] = _fallback,
    access: ReadOnly | Writes | None = None,
    session: Fresh | Keyed | None = None,
    paid: bool = False,
    check: Callable[[BaseModel], str | None] | None = None,
    max_corrections: int = 0,
    filter_skills: bool = False,
    message: str = "Return only the JSON object.",
) -> Role:
    return Role(
        id="testrole",
        template="loops/testrole/prompt.j2",
        reply=reply,
        fallback=fallback,
        context=_TestContext,
        access=access if access is not None else Writes(),
        session=session if session is not None else Fresh(),
        paid=paid,
        check=check,
        max_corrections=max_corrections,
        filter_skills=filter_skills,
        message=message,
    )


async def _spawn(ctx: RunContext, role_id: str = "testrole"):  # noqa: ANN202  # tracked: #288
    return await ctx.agents.spawn(ctx.agents.default_definition(role_id))


def _with_fixture_prompts_dir(tmp_path: Path, fn):  # noqa: ANN001, ANN202  # tracked: #288
    """Patch ``PROMPTS_DIR`` to an isolated fixture tree for one call."""
    prompts_root = tmp_path / "prompts_fixture"
    _write_fixture_templates(prompts_root)
    with patch("vibesys.orchestration.agents.PROMPTS_DIR", prompts_root):
        return fn()


def test_render_uses_role_template_and_context(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply(text="hi"))

    async def body(ctx: RunContext) -> None:
        agent = await _spawn(ctx)
        try:
            await ctx.agents.turn(
                _role(),
                agent=agent,
                context={"subject": "the render test"},
                label="render",
            )
        finally:
            await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))

    call = runner.calls_for("testrole")[0]
    assert call.system_prompt == "Role prompt for the render test.\n"


def test_message_defaults_to_role_message(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply())

    async def body(ctx: RunContext) -> None:
        agent = await _spawn(ctx)
        try:
            await ctx.agents.turn(
                _role(message="the role's own default message"),
                agent=agent,
                context={"subject": "x"},
                label="default-message",
            )
        finally:
            await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))

    call = runner.calls_for("testrole")[0]
    assert call.user_prompt == "the role's own default message"


def test_readonly_revert_of_unauthorized_edit(tmp_path: Path) -> None:
    """A ReadOnly role's unauthorized workspace edit is reverted, not fatal."""
    runner = FakeAgentClient(backend_name="stub")

    async def body(ctx: RunContext) -> None:
        workspace_path = ctx.workspaces.root.path

        def _edit_and_reply(_invocation: object) -> _Reply:
            (workspace_path / "unauthorized.txt").write_text("sneaky edit\n")
            return _Reply()

        runner.enqueue("testrole", _edit_and_reply)
        agent = await _spawn(ctx)
        try:
            await ctx.agents.turn(
                _role(access=ReadOnly()),
                agent=agent,
                context={"subject": "x"},
                label="isolation-revert",
            )
        finally:
            await agent.close()
        assert not (workspace_path / "unauthorized.txt").exists()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))


def test_readonly_allow_list_is_preserved(tmp_path: Path) -> None:
    """A ReadOnly role may still write paths named in ``access.allow``."""
    runner = FakeAgentClient(backend_name="stub")

    async def body(ctx: RunContext) -> None:
        workspace_path = ctx.workspaces.root.path
        (workspace_path / "allowed.txt").write_text("v1\n")
        await ctx.workspaces.root.snapshot("seed-allowed")

        def _edit_allowed(_invocation: object) -> _Reply:
            (workspace_path / "allowed.txt").write_text("v2\n")
            return _Reply()

        runner.enqueue("testrole", _edit_allowed)
        agent = await _spawn(ctx)
        try:
            await ctx.agents.turn(
                _role(access=ReadOnly(allow=("allowed.txt",))),
                agent=agent,
                context={"subject": "x"},
                label="isolation-allow",
            )
        finally:
            await agent.close()
        assert (workspace_path / "allowed.txt").read_text() == "v2\n"

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))


def test_readonly_raises_when_unrevertable(tmp_path: Path) -> None:
    """An untracked (never-committed) edit can't be reverted by git checkout;
    ``ctx.agents.turn`` must raise :class:`RoleIsolationError` rather than
    silently accept it.
    """
    runner = FakeAgentClient(backend_name="stub")

    async def body(ctx: RunContext) -> None:
        runner.enqueue("testrole", _Reply())
        agent = await _spawn(ctx)
        # A restore that can't fully clean the tree (permission error, a
        # nested repo, ...) is a real but hard-to-trigger-portably git
        # condition; simulate it directly by making `pending_changes`
        # unconditionally report one path, so a ReadOnly role's restore
        # attempt still leaves a "remaining" change afterward.
        workspace = ctx.workspaces.root
        original_pending_changes = workspace.pending_changes

        async def _always_dirty() -> list[str]:
            await original_pending_changes()
            return ["always-unauthorized.txt"]

        with patch.object(workspace, "pending_changes", _always_dirty):
            try:
                with pytest.raises(RoleIsolationError):
                    await ctx.agents.turn(
                        _role(access=ReadOnly()),
                        agent=agent,
                        context={"subject": "x"},
                        label="isolation-raise",
                    )
            finally:
                await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))


@pytest.mark.parametrize("access", [ReadOnly(), Writes()])
def test_timeout_falls_back_for_every_access_mode(
    tmp_path: Path, access: ReadOnly | Writes
) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.fail("testrole", subprocess.TimeoutExpired(cmd="agent", timeout=5.0), times=1)

    async def body(ctx: RunContext) -> BaseModel:
        agent = await _spawn(ctx)
        try:
            return await ctx.agents.turn(
                _role(access=access),
                agent=agent,
                context={"subject": "x"},
                label="timeout",
            )
        finally:
            await agent.close()

    reply = _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    assert reply == _fallback()


def test_correction_retries_until_check_passes(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply(text="bad"), _Reply(text="bad"), _Reply(text="ok"))

    def _check(reply: BaseModel) -> str | None:
        assert isinstance(reply, _Reply)
        return None if reply.text == "ok" else "text must be 'ok'"

    async def body(ctx: RunContext) -> BaseModel:
        agent = await _spawn(ctx)
        try:
            return await ctx.agents.turn(
                _role(check=_check, max_corrections=2),
                agent=agent,
                context={"subject": "x"},
                label="correction",
            )
        finally:
            await agent.close()

    reply = _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    assert reply.text == "ok"
    assert len(runner.calls_for("testrole")) == 3


def test_correction_exhaustion_raises(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply(text="bad"), _Reply(text="bad"))

    def _check(reply: BaseModel) -> str | None:
        assert isinstance(reply, _Reply)
        return None if reply.text == "ok" else "text must be 'ok'"

    async def body(ctx: RunContext) -> None:
        agent = await _spawn(ctx)
        try:
            with pytest.raises(CorrectionExhaustedError):
                await ctx.agents.turn(
                    _role(check=_check, max_corrections=1),
                    agent=agent,
                    context={"subject": "x"},
                    label="correction-exhaust",
                )
        finally:
            await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))


def test_correction_message_hook_controls_feedback(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply(text="bad"), _Reply(text="ok"))

    def _check(reply: BaseModel) -> str | None:
        assert isinstance(reply, _Reply)
        return None if reply.text == "ok" else "bad text"

    async def body(ctx: RunContext) -> None:
        agent = await _spawn(ctx)
        try:
            await ctx.agents.turn(
                _role(check=_check, max_corrections=1),
                agent=agent,
                context={"subject": "x"},
                label="correction-hook",
                correction_message=lambda _reply, error: f"custom correction: {error}",
            )
        finally:
            await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    second_call = runner.calls_for("testrole")[1]
    assert second_call.user_prompt == "custom correction: bad text"


def test_paid_hook_runs_before_turn(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply())
    order: list[str] = []

    async def before_paid() -> None:
        order.append("hook")

    async def body(ctx: RunContext) -> None:
        agent = await _spawn(ctx)
        try:
            await ctx.agents.turn(
                _role(paid=True),
                agent=agent,
                context={"subject": "x"},
                label="paid",
                before_paid=before_paid,
            )
        finally:
            await agent.close()
        order.append("turn-done")

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    assert order == ["hook", "turn-done"]


def test_paid_marker_is_committed_before_a_mid_turn_crash(tmp_path: Path) -> None:
    """The paid marker is durable in git even if the agent raises mid-turn.

    ``before_paid`` runs, then the pre-turn snapshot commits its edit,
    *then* the (real or fake) agent call happens. A crash inside that call
    must not lose the marker: the workspace has no pending changes for it
    to lose, because it is already committed.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.fail("testrole", RuntimeError("agent crashed mid-turn"))

    async def body(ctx: RunContext) -> None:
        workspace_path = ctx.workspaces.root.path

        async def before_paid() -> None:
            (workspace_path / "paid-marker.txt").write_text("attempt 1\n")

        agent = await _spawn(ctx)
        try:
            with pytest.raises(RuntimeError, match="agent crashed mid-turn"):
                await ctx.agents.turn(
                    _role(paid=True),
                    agent=agent,
                    context={"subject": "x"},
                    label="paid-crash",
                    before_paid=before_paid,
                )
            # The marker write already landed in a committed snapshot: no
            # pending changes remain to lose if the process dies here.
            assert await ctx.workspaces.root.pending_changes() == []
            assert (workspace_path / "paid-marker.txt").read_text() == "attempt 1\n"
        finally:
            await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))


def test_filter_skills_drops_unknown_selections(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "testrole",
        _SkillReply(
            recommended_skills=[
                SkillResourceSelection(skill="does-not-exist", purpose="test"),
            ]
        ),
    )

    async def body(ctx: RunContext) -> BaseModel:
        agent = await _spawn(ctx)
        try:
            return await ctx.agents.turn(
                _role(reply=_SkillReply, fallback=_SkillReply, filter_skills=True),
                agent=agent,
                context={"subject": "x"},
                label="skills",
            )
        finally:
            await agent.close()

    reply = _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    assert isinstance(reply, _SkillReply)
    # No skill sources are installed in this fixture run, so every selection
    # is dropped (matching `_skills`'s "ignored ... no skills installed").
    assert reply.recommended_skills == []


def test_session_key_reused_across_calls(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply(text="first"), _Reply(text="second"))

    async def body(ctx: RunContext) -> None:
        agent = await _spawn(ctx)
        try:
            role = _role(session=Keyed(scope=SessionScope.HYPOTHESIS))
            await ctx.agents.turn(
                role, agent=agent, context={"subject": "x"}, label="k1", session_key="H-01"
            )
            await ctx.agents.turn(
                role, agent=agent, context={"subject": "x"}, label="k2", session_key="H-01"
            )
        finally:
            await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    calls = runner.calls_for("testrole")
    assert calls[0].session_key == calls[1].session_key
    assert calls[0].reuse_session is True


# --------------------------------------------------------------------------
# Hypothesis property test
#
# Scope note (see the phase-3a report): this property test covers the
# correction loop (random valid/invalid reply sequences), the invariant that
# ``check`` never accepts more than ``max_corrections`` retries, and that a
# turn never returns a reply ``check`` rejects. Timeout-fallback and
# unauthorized-workspace-edit invariants are covered by the deterministic
# tests above (``test_timeout_falls_back_for_every_access_mode``,
# ``test_readonly_revert_of_unauthorized_edit``,
# ``test_readonly_raises_when_unrevertable``) rather than folded into this
# same randomized sequence, to keep the scripted-client wiring robust.
# --------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(
    valid_at=st.integers(min_value=0, max_value=5),
    max_corrections=st.integers(min_value=0, max_value=5),
)
def test_correction_loop_invariants_under_random_valid_position(
    tmp_path_factory: pytest.TempPathFactory, valid_at: int, max_corrections: int
) -> None:
    """A valid reply arrives at a random position in the queue (0-indexed);
    every position before it is invalid. Invariants: a returned reply always
    passes ``check`` (or the loop raised :class:`CorrectionExhaustedError`),
    and the number of turns taken is always ``<= max_corrections + 1``.
    """
    tmp_path = tmp_path_factory.mktemp("correction-invariants")
    runner = FakeAgentClient(backend_name="stub")
    responses = [_Reply(text=f"bad-{i}") for i in range(valid_at)]
    responses.append(_Reply(text="ok"))
    runner.enqueue("testrole", *responses)

    def _check(reply: BaseModel) -> str | None:
        assert isinstance(reply, _Reply)
        return None if reply.text == "ok" else "must be ok"

    async def body(ctx: RunContext) -> BaseModel | Exception:
        agent = await _spawn(ctx)
        try:
            return await ctx.agents.turn(
                _role(check=_check, max_corrections=max_corrections),
                agent=agent,
                context={"subject": "x"},
                label="property",
            )
        except CorrectionExhaustedError as error:
            return error
        finally:
            await agent.close()

    result = _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    calls_made = len(runner.calls_for("testrole"))

    if valid_at <= max_corrections:
        assert isinstance(result, _Reply)
        assert _check(result) is None
    else:
        assert isinstance(result, CorrectionExhaustedError)
    assert calls_made <= max_corrections + 1
