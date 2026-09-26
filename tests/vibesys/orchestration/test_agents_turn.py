"""Unit tests for ``ctx.agents.turn`` (``vibesys.orchestration.runtime._Agents.turn``).

Exercises the mechanics ``Role`` declares against a synthetic fixture
template (rendering, isolation revert/raise, timeout fallback, correction
retries + exhaustion, skill filtering, session policies, post-turn snapshot
gating), plus a Hypothesis property test over random behavior sequences.
Every strategy's real role prompts are covered by ``tests/vibesys/golden/``,
which snapshots the exact rendered text a scripted end-to-end run sends;
there is no separate hand-rolled rendering path left in ``turns.py`` to
differential-test against ``ctx.agents.turn`` (every strategy already
renders exclusively through it).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch  # test-isolation: workspace methods patched below

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel
from tests.vibesys.orchestration.harness import run_with_context

from vibesys.context import RunSetup
from vibesys.runtime import (
    CorrectionExhaustedError,
    Fresh,
    Keyed,
    ReadOnly,
    Reuse,
    Role,
    RoleIsolationError,
    Writes,
)
from vibesys.schemas import SkillResourceSelection
from vs_agent.api import AgentSessionKey, SessionScope
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


def _role(  # noqa: PLR0913  # LW-040115 [PLR0913]; test helper mirroring every Role field.
    *,
    reply: type[BaseModel] = _Reply,
    fallback: Callable[[], BaseModel] = _fallback,
    access: ReadOnly | Writes | None = None,
    session: Fresh | Keyed | Reuse | None = None,
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


async def _spawn(ctx: RunContext, role_id: str = "testrole"):  # noqa: ANN202  # LW-040116 [ANN202]; the helper is private to this test module and its return type is the local closure type.
    return await ctx.agents.spawn(ctx.agents.default_definition(role_id))


def _with_fixture_prompts_dir(tmp_path: Path, fn):  # noqa: ANN001, ANN202  # LW-040117 [ANN001, ANN202]; the helper wraps an arbitrary test callable, so its parameter and return types are open.
    """Patch ``PROMPTS_DIR`` to an isolated fixture tree for one call."""
    prompts_root = tmp_path / "prompts_fixture"
    _write_fixture_templates(prompts_root)
    # test-isolation: PROMPTS_DIR is a module constant with no injection seam; the test points it at fixture templates
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

        # test-isolation: the test swaps one workspace method to script a dirty or tracked state, with no fake workspace yet
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


def test_readonly_revert_reverts_stray_write_in_declared_memory_path(tmp_path: Path) -> None:
    """A ReadOnly role's stray write inside a declared-memory path is fully
    reverted, the same as any other unauthorized edit.

    Regression test for a live run crash: the judge role (``ReadOnly()``, no
    ``access.allow``) wrote its own evidence JSON under
    ``progress-artifacts/evidence/...``, a declared-memory path. Because
    ``workspaces.restore`` preserves declared-memory paths by default, the
    stray write survived the isolation restore; the post-restore
    unauthorized-paths check then saw it as "still modified" and raised
    ``RoleIsolationError``, crashing the run. Before the fix (``restore``
    always preserving memory during isolation reverts), this test fails
    with ``RoleIsolationError``; after the fix (isolation revert passes
    ``preserve_memory=False``), the stray write is fully reverted and the
    turn completes normally.
    """
    runner = FakeAgentClient(backend_name="stub")

    async def body(ctx: RunContext) -> None:
        workspace_path = ctx.workspaces.root.path
        evidence_path = workspace_path / "progress-artifacts" / "evidence" / "round-1-judge.json"

        def _write_evidence(_invocation: object) -> _Reply:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text('{"verdict": "pass"}\n')
            return _Reply()

        runner.enqueue("testrole", _write_evidence)
        agent = await _spawn(ctx)
        try:
            await ctx.agents.turn(
                _role(access=ReadOnly()),
                agent=agent,
                context={"subject": "x"},
                label="isolation-memory-revert",
            )
        finally:
            await agent.close()
        assert not evidence_path.exists()

    setup = RunSetup(memory_paths=("progress-artifacts",))
    _with_fixture_prompts_dir(
        tmp_path, lambda: run_with_context(tmp_path, runner, body, setup=setup)
    )


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


def test_fresh_session_never_reuses(tmp_path: Path) -> None:
    """``Fresh`` always passes ``reuse_session=False`` and no session key."""
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply(), _Reply())

    async def body(ctx: RunContext) -> None:
        agent = await _spawn(ctx)
        try:
            role = _role(session=Fresh())
            await ctx.agents.turn(role, agent=agent, context={"subject": "x"}, label="f1")
            await ctx.agents.turn(role, agent=agent, context={"subject": "x"}, label="f2")
        finally:
            await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    calls = runner.calls_for("testrole")
    assert calls[0].reuse_session is False
    assert calls[1].reuse_session is False
    # No explicit key is passed; the client falls back to its own per-role
    # default key, which does not enable reuse without reuse_session=True.
    assert calls[0].session_key == AgentSessionKey(SessionScope.ROLE, "testrole")
    assert calls[1].session_key == AgentSessionKey(SessionScope.ROLE, "testrole")


def test_reuse_session_defers_to_agent_client_default(tmp_path: Path) -> None:
    """``Reuse`` passes ``reuse_session=None`` and no explicit session key,
    letting the client apply its own per-role default session.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply(), _Reply())

    async def body(ctx: RunContext) -> None:
        agent = await _spawn(ctx)
        try:
            role = _role(session=Reuse())
            await ctx.agents.turn(role, agent=agent, context={"subject": "x"}, label="r1")
            await ctx.agents.turn(role, agent=agent, context={"subject": "x"}, label="r2")
        finally:
            await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    calls = runner.calls_for("testrole")
    assert calls[0].reuse_session is None
    assert calls[1].reuse_session is None
    assert calls[0].session_key == AgentSessionKey(SessionScope.ROLE, "testrole")


def test_post_turn_snapshot_skipped_for_readonly_with_no_changes(tmp_path: Path) -> None:
    """A ``ReadOnly`` role that makes no edits triggers only the pre-turn
    snapshot; the post-turn snapshot is skipped (nothing to record).
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply())
    calls: list[str] = []

    async def body(ctx: RunContext) -> None:
        agent = await _spawn(ctx)
        workspace = ctx.workspaces.root
        original_snapshot = workspace.snapshot

        async def _tracked_snapshot(label: str) -> str:
            calls.append(label)
            return await original_snapshot(label)

        # test-isolation: the test swaps one workspace method to script a dirty or tracked state, with no fake workspace yet
        with patch.object(workspace, "snapshot", _tracked_snapshot):
            try:
                await ctx.agents.turn(
                    _role(access=ReadOnly()),
                    agent=agent,
                    context={"subject": "x"},
                    label="no-op",
                )
            finally:
                await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    assert calls == ["no-op-input"]


def test_post_turn_snapshot_always_runs_for_writes(tmp_path: Path) -> None:
    """A ``Writes`` role always snapshots after the turn, even with no edits:
    a later round or gate may need to check out this revision regardless.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("testrole", _Reply())
    calls: list[str] = []

    async def body(ctx: RunContext) -> None:
        agent = await _spawn(ctx)
        workspace = ctx.workspaces.root
        original_snapshot = workspace.snapshot

        async def _tracked_snapshot(label: str) -> str:
            calls.append(label)
            return await original_snapshot(label)

        # test-isolation: the test swaps one workspace method to script a dirty or tracked state, with no fake workspace yet
        with patch.object(workspace, "snapshot", _tracked_snapshot):
            try:
                await ctx.agents.turn(
                    _role(access=Writes()),
                    agent=agent,
                    context={"subject": "x"},
                    label="write-no-op",
                )
            finally:
                await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    assert calls == ["write-no-op-input", "write-no-op"]


def test_post_turn_snapshot_runs_when_readonly_role_has_pending_changes(tmp_path: Path) -> None:
    """A ``ReadOnly`` role with edits allowed under ``access.allow`` still
    snapshots after the turn: the allowed change is real and must be
    recorded.
    """
    runner = FakeAgentClient(backend_name="stub")
    calls: list[str] = []

    async def body(ctx: RunContext) -> None:
        workspace_path = ctx.workspaces.root.path
        (workspace_path / "allowed.txt").write_text("v1\n")
        await ctx.workspaces.root.snapshot("seed-allowed")

        def _edit_allowed(_invocation: object) -> _Reply:
            (workspace_path / "allowed.txt").write_text("v2\n")
            return _Reply()

        runner.enqueue("testrole", _edit_allowed)
        agent = await _spawn(ctx)
        workspace = ctx.workspaces.root
        original_snapshot = workspace.snapshot

        async def _tracked_snapshot(label: str) -> str:
            calls.append(label)
            return await original_snapshot(label)

        # test-isolation: the test swaps one workspace method to script a dirty or tracked state, with no fake workspace yet
        with patch.object(workspace, "snapshot", _tracked_snapshot):
            try:
                await ctx.agents.turn(
                    _role(access=ReadOnly(allow=("allowed.txt",))),
                    agent=agent,
                    context={"subject": "x"},
                    label="allowed-edit",
                )
            finally:
                await agent.close()

    _with_fixture_prompts_dir(tmp_path, lambda: run_with_context(tmp_path, runner, body))
    assert calls == ["allowed-edit-input", "allowed-edit"]


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


def _seed_tracked_paths(workspace_path: Path, paths: list[str], tracked_flags: list[bool]) -> None:
    """Pre-create (uncommitted) content for every path marked ``tracked``."""
    for path, tracked in zip(paths, tracked_flags, strict=True):
        if tracked:
            full = workspace_path / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text("original\n")


def _assert_isolation_invariants(
    workspace_path: Path,
    paths: list[str],
    tracked_flags: list[bool],
    allowed_flags: list[bool],
    error: RoleIsolationError | None,
) -> None:
    for path, allowed in zip(paths, allowed_flags, strict=True):
        if allowed:
            # Never leaves an allowed path reverted.
            assert (workspace_path / path).read_text() == "stray\n"

    # A genuine git working tree can always revert both a tracked
    # modification and a brand-new untracked file, so this must never
    # raise; when it does, that is exactly the bug under test (a
    # declared-memory path silently surviving the restore and then
    # failing the post-restore unauthorized check).
    assert error is None

    for path, tracked, allowed in zip(paths, tracked_flags, allowed_flags, strict=True):
        if allowed:
            continue
        # Reverts every unauthorized write, memory path or not.
        full = workspace_path / path
        if tracked:
            assert full.read_text() == "original\n"
        else:
            assert not full.exists()


@settings(max_examples=40, deadline=None)
@given(
    specs=st.lists(
        st.tuples(st.booleans(), st.booleans(), st.booleans()),
        min_size=1,
        max_size=4,
    )
)
def test_readonly_isolation_reverts_every_unauthorized_stray_write(
    tmp_path_factory: pytest.TempPathFactory,
    specs: list[tuple[bool, bool, bool]],
) -> None:
    """For any set of stray writes a ``ReadOnly`` role makes across tracked,
    untracked, and declared-memory paths, ``ctx.agents.turn`` reverts every
    write the role was not authorized to make -- declared-memory paths are
    not a loophole -- and never touches a path named in ``access.allow``.

    Each generated spec is ``(in_memory, tracked, allowed)`` for one stray
    write: *in_memory* puts it under the declared-memory prefix
    ``progress-artifacts/``, *tracked* means the file already existed
    (committed) before the turn so the stray write is a tracked
    modification rather than a brand-new untracked file, and *allowed*
    lists it in the role's ``access.allow``.
    """
    tmp_path = tmp_path_factory.mktemp("isolation-property")
    runner = FakeAgentClient(backend_name="stub")

    paths = [
        f"progress-artifacts/evidence-{i}.json" if in_memory else f"evidence-{i}.json"
        for i, (in_memory, _tracked, _allowed) in enumerate(specs)
    ]
    tracked_flags = [tracked for _in_memory, tracked, _allowed in specs]
    allowed_flags = [allowed for _in_memory, _tracked, allowed in specs]
    allow = tuple(path for path, allowed in zip(paths, allowed_flags, strict=True) if allowed)

    async def body(ctx: RunContext) -> RoleIsolationError | None:
        workspace_path = ctx.workspaces.root.path
        _seed_tracked_paths(workspace_path, paths, tracked_flags)
        if any(tracked_flags):
            await ctx.workspaces.root.snapshot("seed-tracked")

        def _stray_writes(_invocation: object) -> _Reply:
            for path in paths:
                full = workspace_path / path
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text("stray\n")
            return _Reply()

        runner.enqueue("testrole", _stray_writes)
        agent = await _spawn(ctx)
        error: RoleIsolationError | None = None
        try:
            await ctx.agents.turn(
                _role(access=ReadOnly(allow=allow)),
                agent=agent,
                context={"subject": "x"},
                label="isolation-property",
            )
        except RoleIsolationError as caught:
            error = caught
        finally:
            await agent.close()

        _assert_isolation_invariants(workspace_path, paths, tracked_flags, allowed_flags, error)
        return error

    setup = RunSetup(memory_paths=("progress-artifacts",))
    _with_fixture_prompts_dir(
        tmp_path, lambda: run_with_context(tmp_path, runner, body, setup=setup)
    )
