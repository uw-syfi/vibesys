"""The generic operation executor over real receipts, owners and filesystem state."""

from __future__ import annotations

import contextlib
import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.observation_contract import assert_core_accepts
from tests.support.runtime_operations import (
    SCENARIO_NAMES,
    SCOPE,
    EchoOwner,
    EchoRequest,
    OperationScenario,
    RenderRoleArtifacts,
    SimulatedCrashError,
    VerifyParentRevision,
    catalog_of,
    commit_of,
    execute_request,
    scenarios,
)
from tests.support.runtime_operations import revision as revision_ref

from vs_core.api import (
    CancelOwnedResource,
    ContractError,
    ExecuteRegisteredOperation,
    HostFence,
    HostId,
    InspectRequest,
    ObservationStatus,
    OperationRegistry,
    RequestId,
    RequestObserved,
    ResourceId,
    RevisionId,
    RevisionRef,
)
from vs_project.api import Project
from vs_runtime.api.core import (
    REQUEST_DISPATCH,
    ExecutionContext,
    ExecutionResult,
    ExecutorRole,
    IntentReceipt,
    NamespaceOperationReceipts,
    ObservationFactory,
    OperationCatalog,
    OperationEntry,
    ReceiptStore,
    RegisteredOperationRequests,
    RequestExecutors,
    ResultReceipt,
    VerifyRevisionOwner,
)
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaces

pytestmark = pytest.mark.asyncio

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from vs_core.api import Request
    from vs_project.api import StateNamespace

FENCE = HostFence(host_id=HostId(root="host"), epoch=1)
POINTS = ("before_intent", "after_intent", "before_result", "after_result")


def context_for(request: Request) -> ExecutionContext:
    return ExecutionContext(
        fence=FENCE,
        now_at=5.0,
        payload_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
    )


class CrashingReceipts(NamespaceOperationReceipts):
    """Real receipts that kill the process at one named boundary."""

    def __init__(self, namespace: StateNamespace, crash_at: str | None) -> None:
        super().__init__(namespace)
        self._crash_at = crash_at

    def _maybe(self, point: str) -> None:
        if self._crash_at == point:
            raise SimulatedCrashError

    def record_intent(self, receipt: IntentReceipt) -> None:
        self._maybe("before_intent")
        super().record_intent(receipt)
        self._maybe("after_intent")

    def record_result(self, receipt: ResultReceipt) -> None:
        self._maybe("before_result")
        super().record_result(receipt)
        self._maybe("after_result")


@contextmanager
def workspace() -> Iterator[tuple[Path, StateNamespace]]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "project").mkdir()
        project = Project.open(root / "project")
        yield root, project.state.state_store_namespace("run")


def executor(
    items: tuple[OperationScenario, ...], namespace: StateNamespace, crash_at: str | None = None
) -> RegisteredOperationRequests:
    return RegisteredOperationRequests(
        catalog_of(items),
        CrashingReceipts(namespace, crash_at),
        ObservationFactory(ReceiptStore(namespace)),
    )


def observed(result: ExecutionResult) -> RequestObserved:
    return result.observation


def pick(items: tuple[OperationScenario, ...], name: str) -> OperationScenario:
    return next(item for item in items if item.name == name)


async def test_every_scenario_name_is_listed_so_none_escapes_the_shared_tests() -> None:
    with workspace() as (root, namespace):
        assert tuple(item.name for item in scenarios(root, namespace)) == SCENARIO_NAMES


async def baseline(name: str) -> tuple[RequestObserved, int]:
    """A crash-free run of one scenario, for comparison."""
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        scenario = pick(items, name)
        request = execute_request(catalog_of(items), scenario.request, f"req-{name}")
        result = await executor(items, namespace).execute(request, context_for(request))
        return observed(result), scenario.effects()


@pytest.mark.parametrize("name", SCENARIO_NAMES)
@pytest.mark.parametrize("crash_at", POINTS)
async def test_crash_at_every_boundary_then_restart_yields_one_effect_and_the_same_result(
    name: str, crash_at: str
) -> None:
    expected, expected_effects = await baseline(name)
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        scenario = pick(items, name)
        request = execute_request(catalog_of(items), scenario.request, f"req-{name}")
        context = context_for(request)
        crashed = False
        try:
            await executor(items, namespace, crash_at).execute(request, context)
        except SimulatedCrashError:
            crashed = True
        assert crashed or scenario.refused
        restarted = executor(items, namespace)
        result = await restarted.execute(request, context)
        assert observed(result) == expected
        assert scenario.effects() == expected_effects
        if not scenario.allows_repeat:
            assert scenario.effects() <= 1
        again = await restarted.execute(request, context)
        assert observed(again) == expected
        assert scenario.effects() == expected_effects


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    name=st.sampled_from(SCENARIO_NAMES),
    crashes=st.lists(st.sampled_from(POINTS), max_size=4),
)
async def test_any_sequence_of_crashes_leaves_at_most_one_effect(
    name: str, crashes: list[str]
) -> None:
    expected, expected_effects = await baseline(name)
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        scenario = pick(items, name)
        request = execute_request(catalog_of(items), scenario.request, f"req-{name}")
        context = context_for(request)
        for crash_at in crashes:
            try:
                await executor(items, namespace, crash_at).execute(request, context)
            except SimulatedCrashError:
                continue
        result = await executor(items, namespace).execute(request, context)
        assert observed(result) == expected
        assert scenario.effects() == expected_effects


@pytest.mark.parametrize("name", [n for n in SCENARIO_NAMES if n not in ("interpret", "retain")])
async def test_success_carries_the_validated_registered_outcome_and_claims_release(
    name: str,
) -> None:
    result, _ = await baseline(name)
    assert result.observation.status is ObservationStatus.SUCCEEDED
    assert result.observation.terminal
    assert result.observation.accepted
    assert result.outcome is not None
    assert result.outcome_json is not None


@pytest.mark.parametrize("name", ["interpret", "retain"])
async def test_declared_but_refused_operations_reject_with_their_typed_reason(name: str) -> None:
    result, effects = await baseline(name)
    assert result.observation.status is ObservationStatus.REJECTED
    assert result.outcome is None
    assert "declared but refused" in result.observation.diagnostic
    assert effects == 0


async def test_unknown_operation_is_rejected_without_receipt_or_effect() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        echo = pick(items, "echo")
        full = catalog_of(items)
        request = execute_request(full, echo.request, "req-foreign")
        smaller = tuple(item for item in items if item.name != "echo")
        receipts = NamespaceOperationReceipts(namespace)
        result = await RegisteredOperationRequests(
            catalog_of(smaller), receipts, ObservationFactory(ReceiptStore(namespace))
        ).execute(request, context_for(request))
        assert observed(result).observation.status is ObservationStatus.REJECTED
        assert "not in the catalog" in observed(result).observation.diagnostic
        assert receipts.intent("req-foreign") is None
        assert echo.effects() == 0


async def test_a_schema_version_the_catalog_does_not_declare_is_rejected() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        request = execute_request(catalog_of(items), pick(items, "echo").request, "req-old")
        schema = request.operation.schema_ref
        old = schema.model_copy(
            update={"request_schema": schema.request_schema.model_copy(update={"version": 0})}
        )
        stale = request.model_copy(
            update={"operation": request.operation.model_copy(update={"schema_ref": old})}
        )
        result = await executor(items, namespace).execute(stale, context_for(stale))
        assert observed(result).observation.status is ObservationStatus.REJECTED
        assert pick(items, "echo").effects() == 0


async def test_output_that_violates_the_declared_schema_is_sealed_failed_and_never_rerun() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        echo = pick(items, "echo")
        owner = echo.entry.owner
        assert isinstance(owner, EchoOwner)
        owner.output = {"status": "succeeded", "text": 3, "extra": True}
        request = execute_request(catalog_of(items), echo.request, "req-bad")
        runner = executor(items, namespace)
        first = await runner.execute(request, context_for(request))
        assert observed(first).observation.status is ObservationStatus.FAILED
        assert observed(first).outcome is None
        assert "violates declared schema" in observed(first).observation.diagnostic
        owner.output = None
        second = await executor(items, namespace).execute(request, context_for(request))
        assert observed(second) == observed(first)
        assert echo.effects() == 1


async def test_an_owner_of_another_outcome_model_is_rejected_by_type() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        echo = pick(items, "echo")
        owner = echo.entry.owner
        assert isinstance(owner, EchoOwner)
        owner.output = EchoRequest(text="not an outcome")
        request = execute_request(catalog_of(items), echo.request, "req-type")
        result = await executor(items, namespace).execute(request, context_for(request))
        assert observed(result).observation.status is ObservationStatus.FAILED


async def test_an_owner_exception_is_unknown_and_a_later_restart_does_not_repeat_the_effect() -> (
    None
):
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        echo = pick(items, "echo")
        owner = echo.entry.owner
        assert isinstance(owner, EchoOwner)
        owner.hide_effects = True
        request = execute_request(catalog_of(items), echo.request, "req-unknown")
        runner = executor(items, namespace)
        first = await runner.execute(request, context_for(request))
        assert observed(first).observation.status is ObservationStatus.SUCCEEDED
        # Remove the result so only the intent remains, as after a crash before it.
        path = namespace.external_directory("operations")
        for item in path.glob("*.result.json"):
            item.unlink()
        for _ in range(3):
            again = await executor(items, namespace).execute(request, context_for(request))
            unknown = observed(again).observation
            assert unknown.status is ObservationStatus.UNKNOWN
            assert not unknown.terminal
            assert not unknown.released
            assert not unknown.accepted
        assert echo.effects() == 1


async def test_same_identity_with_another_payload_is_a_rejected_observation() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        echo = pick(items, "echo")
        request = execute_request(catalog_of(items), echo.request, "req-same")
        runner = executor(items, namespace)
        await runner.execute(request, context_for(request))
        other = ExecutionContext(fence=FENCE, now_at=5.0, payload_digest="different")
        rejected = observed(await runner.execute(request, other)).observation
        assert rejected.status is ObservationStatus.REJECTED
        assert "another payload" in rejected.diagnostic
        assert echo.effects() == 1


async def test_catalog_rejects_missing_duplicate_and_unregistered_entries() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        registry = OperationRegistry(tuple(item.entry.registration for item in items))
        entries = tuple(item.entry for item in items)
        with pytest.raises(ContractError, match="without entry"):
            OperationCatalog(registry, entries[1:])
        with pytest.raises(ContractError, match="duplicate"):
            OperationCatalog(registry, (*entries, entries[0]))
        with pytest.raises(ContractError, match="not registered"):
            OperationCatalog(OperationRegistry(), entries[:1])
        with pytest.raises(ContractError, match="exactly one"):
            OperationEntry(entries[0].registration)


async def inspect_of(
    runner: RegisteredOperationRequests, target: str, *, resource: str | None = None
) -> RequestObserved:
    request = InspectRequest(
        request_id=RequestId(root=f"inspect:{target}"),
        scope=SCOPE,
        deadline_at=100.0,
        target=RequestId(root=target),
        resource_id=None if resource is None else ResourceId(root=resource),
    )
    return observed(await runner.execute(request, context_for(request)))


@pytest.mark.parametrize("name", [n for n in SCENARIO_NAMES if n not in ("interpret", "retain")])
@pytest.mark.parametrize("crash_at", POINTS)
async def test_inspection_after_a_crash_reports_the_target_without_a_second_effect(
    name: str, crash_at: str
) -> None:
    expected, expected_effects = await baseline(name)
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        scenario = pick(items, name)
        request = execute_request(catalog_of(items), scenario.request, f"req-{name}")
        with contextlib.suppress(SimulatedCrashError):
            await executor(items, namespace, crash_at).execute(request, context_for(request))
        query = await inspect_of(executor(items, namespace), f"req-{name}")
        assert query.observation.status is ObservationStatus.SUCCEEDED
        assert query.target is not None
        if crash_at == "before_intent":
            # Nothing proves the target was ever started, so the answer is Unknown.
            assert query.target.observation.status is ObservationStatus.UNKNOWN
            assert scenario.effects() == 0
            return
        if crash_at == "after_intent" and not scenario.allows_repeat:
            # The owner proves the effect never ran, so inspection seals a typed rejection.
            assert query.target.observation.status is ObservationStatus.REJECTED
            assert scenario.effects() == 0
            replay = await executor(items, namespace).execute(request, context_for(request))
            assert observed(replay).observation.status is ObservationStatus.REJECTED
            assert scenario.effects() == 0
            return
        assert query.target.observation.status is ObservationStatus.SUCCEEDED
        assert query.target.outcome == expected.outcome
        assert scenario.effects() <= max(expected_effects, 1)
        replay = await executor(items, namespace).execute(request, context_for(request))
        assert observed(replay) == expected
        assert scenario.effects() == expected_effects


async def test_inspection_routes_unknown_targets_and_child_resources() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        runner = executor(items, namespace)
        missing = await inspect_of(runner, "nobody")
        assert missing.target is not None
        assert missing.target.observation.status is ObservationStatus.UNKNOWN
        child = await inspect_of(runner, "nobody", resource="child")
        assert child.observation.status is ObservationStatus.REJECTED
        assert child.target is None


async def test_inspection_seals_an_effect_the_owner_proves_never_happened() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        echo = pick(items, "echo")
        request = execute_request(catalog_of(items), echo.request, "req-never")
        receipts = NamespaceOperationReceipts(namespace)
        receipts.record_intent(
            IntentReceipt(
                request_id="req-never",
                payload_digest=context_for(request).payload_digest,
                operation=request.operation,
            )
        )
        runner = RegisteredOperationRequests(
            catalog_of(items), receipts, ObservationFactory(ReceiptStore(namespace))
        )
        query = await inspect_of(runner, "req-never")
        assert query.target is not None
        assert query.target.observation.status is ObservationStatus.REJECTED
        replay = await runner.execute(request, context_for(request))
        assert observed(replay).observation.status is ObservationStatus.REJECTED
        assert echo.effects() == 0


def cancel_request(target: str, name: str) -> CancelOwnedResource:
    return CancelOwnedResource(
        request_id=RequestId(root=name),
        scope=SCOPE,
        deadline_at=100.0,
        resource_id=ResourceId(root="resource"),
        target=RequestId(root=target),
    )


async def test_cancel_routes_to_the_owner_once_and_never_claims_release() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        echo = pick(items, "echo")
        owner = echo.entry.owner
        assert isinstance(owner, EchoOwner)
        request = execute_request(catalog_of(items), echo.request, "req-run")
        runner = executor(items, namespace)
        await runner.execute(request, context_for(request))
        cancel = cancel_request("req-run", "cancel-1")
        first = observed(await runner.execute(cancel, context_for(cancel)))
        second = observed(await executor(items, namespace).execute(cancel, context_for(cancel)))
        assert first == second
        assert first.observation.status is ObservationStatus.CANCELLED
        assert first.observation.resource_id == ResourceId(root="resource")
        assert not first.observation.released
        assert owner.cancelled == ["one"]


async def test_cancel_of_a_non_cancellable_or_unknown_target_does_not_pretend() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        request = execute_request(catalog_of(items), pick(items, "render").request, "req-r")
        runner = executor(items, namespace)
        await runner.execute(request, context_for(request))
        refused = cancel_request("req-r", "cancel-r")
        result = observed(await runner.execute(refused, context_for(refused)))
        assert result.observation.status is ObservationStatus.REJECTED
        assert not result.observation.released
        unknown = cancel_request("nobody", "cancel-n")
        result = observed(await runner.execute(unknown, context_for(unknown)))
        assert result.observation.status is ObservationStatus.UNKNOWN
        assert not result.observation.released


async def test_request_executors_route_every_operation_role_request_here() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        runner = executor(items, namespace)
        executors = RequestExecutors(operations=runner)
        request = execute_request(catalog_of(items), pick(items, "echo").request, "req-wired")
        assert executors.refusal(request) is None
        outcome = await executors.dispatch(request, context_for(request))
        assert isinstance(outcome, ExecutionResult)
        assert isinstance(request, ExecuteRegisteredOperation)


@pytest.mark.parametrize(
    ("commit", "digest", "verified"),
    [("abc", "git-commit:abc", True), ("zzz", "git-commit:zzz", False), ("abc", "other", False)],
)
async def test_parent_verification_requires_a_retained_canonical_revision(
    commit: str, digest: str, *, verified: bool
) -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        parent = RevisionRef(revision_id=RevisionId(root=commit), digest=digest)
        request = execute_request(catalog_of(items), VerifyParentRevision(parent=parent), "req-v")
        result = observed(await executor(items, namespace).execute(request, context_for(request)))
        assert result.observation.status is ObservationStatus.SUCCEEDED
        assert getattr(result.outcome, "verified", None) is verified


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    retained=st.sets(st.sampled_from(["r1", "r2", "r3"])),
    dangling=st.sets(st.sampled_from(["d1", "d2", "d3"])),
    asked=st.sampled_from(["r1", "r2", "r3", "d1", "d2", "d3", "unknown"]),
)
async def test_parent_verification_checks_retention_not_that_the_revision_exports(
    retained: set[str], dangling: set[str], asked: str
) -> None:
    """A commit that exists but is not retained (dangling) must not verify."""
    workspaces = FakeWorkspaces(FakeWorkspace(known_revisions=retained))
    for revision in dangling:
        workspaces.add_dangling_revision(revision)
    owner = VerifyRevisionOwner(workspaces, workspaces, commit_of)
    request = VerifyParentRevision(parent=revision_ref(asked))
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        wire = execute_request(catalog_of(items), request, "req-r")
        outcome = await owner.execute(request, context_for(wire))
    expected = asked in retained and asked not in dangling
    assert outcome["verified"] is expected
    if asked in dangling and asked not in retained:
        assert await workspaces.export_patch(asked)  # present, yet not verified


async def test_render_with_a_missing_template_variable_is_a_typed_failure_with_no_artifact() -> (
    None
):
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        (root / "templates" / "greeting.j2").write_text("hello {{ who }} {{ missing }}\n")
        render = pick(items, "render")
        request = execute_request(catalog_of(items), render.request, "req-missing")
        assert isinstance(render.request, RenderRoleArtifacts)
        result = observed(await executor(items, namespace).execute(request, context_for(request)))
        assert result.observation.status is ObservationStatus.SUCCEEDED
        assert getattr(result.outcome, "status", None) == "failed"
        assert render.effects() == 0


async def test_render_is_idempotent_per_subject_and_ordinal_and_content_addressed() -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        render = pick(items, "render")
        runner = executor(items, namespace)
        first = execute_request(catalog_of(items), render.request, "req-a")
        second = execute_request(catalog_of(items), render.request, "req-b")
        a = observed(await runner.execute(first, context_for(first)))
        b = observed(await runner.execute(second, context_for(second)))
        assert getattr(a.outcome, "prompts", None) == getattr(b.outcome, "prompts", ())
        assert render.effects() == 1


# Observation contract: core accepts every output across a retry and restarts -----------------


async def _unknown_then_executed_then_replayed() -> list[ExecutionResult]:
    """The owner loses its connection once; the retry after a restart then succeeds."""
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        echo = pick(items, "echo")
        owner = echo.entry.owner
        assert isinstance(owner, EchoOwner)
        owner.failures_left = 1
        request = execute_request(catalog_of(items), echo.request, "req-run")
        context = context_for(request)
        first = await executor(items, namespace).execute(request, context)
        assert observed(first).observation.status is ObservationStatus.UNKNOWN
        retried = await executor(items, namespace).execute(request, context)
        assert observed(retried).observation.status is ObservationStatus.SUCCEEDED
        replayed = await executor(items, namespace).execute(request, context)
        return [first, retried, replayed]


async def _inspect_before_and_after_the_target_ran() -> list[ExecutionResult]:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        echo = pick(items, "echo")
        request = execute_request(catalog_of(items), echo.request, "req-run")
        inspect = InspectRequest(
            request_id=RequestId(root="inspect-run"),
            scope=SCOPE,
            deadline_at=100.0,
            target=RequestId(root="req-run"),
        )
        before = await executor(items, namespace).execute(inspect, context_for(inspect))
        assert before.observation.target is not None
        assert before.observation.target.observation.status is ObservationStatus.UNKNOWN
        ran = await executor(items, namespace).execute(request, context_for(request))
        after = await executor(items, namespace).execute(inspect, context_for(inspect))
        assert after.observation.target is not None
        assert after.observation.target.observation.status is ObservationStatus.SUCCEEDED
        again = await executor(items, namespace).execute(inspect, context_for(inspect))
        return [before, ran, after, again]


async def _cancel_before_and_after_the_target_ran() -> list[ExecutionResult]:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        echo = pick(items, "echo")
        request = execute_request(catalog_of(items), echo.request, "req-run")
        cancel = cancel_request("req-run", "cancel-run")
        before = await executor(items, namespace).execute(cancel, context_for(cancel))
        assert observed(before).observation.status is ObservationStatus.UNKNOWN
        ran = await executor(items, namespace).execute(request, context_for(request))
        after = await executor(items, namespace).execute(cancel, context_for(cancel))
        assert observed(after).observation.status is ObservationStatus.CANCELLED
        again = await executor(items, namespace).execute(cancel, context_for(cancel))
        return [before, ran, after, again]


# One scenario per request kind of the operations role; the test below keeps this complete.
OBSERVATION_SCENARIOS: dict[type, Callable[[], Awaitable[list[ExecutionResult]]]] = {
    ExecuteRegisteredOperation: _unknown_then_executed_then_replayed,
    InspectRequest: _inspect_before_and_after_the_target_ran,
    CancelOwnedResource: _cancel_before_and_after_the_target_ran,
}


async def test_every_operation_request_kind_has_an_observation_scenario() -> None:
    routed = {kind for kind, role in REQUEST_DISPATCH.items() if role is ExecutorRole.OPERATIONS}
    assert set(OBSERVATION_SCENARIOS) == routed


@pytest.mark.parametrize("kind", list(OBSERVATION_SCENARIOS), ids=lambda kind: kind.__name__)
async def test_core_accepts_a_retry_after_unknown_and_replays_across_restarts(
    kind: type,
) -> None:
    assert_core_accepts(await OBSERVATION_SCENARIOS[kind]())


@pytest.mark.parametrize("name", [n for n in SCENARIO_NAMES if n != "echo"])
async def test_same_identity_with_another_payload_is_rejected_after_every_registered_result(
    name: str,
) -> None:
    with workspace() as (root, namespace):
        items = scenarios(root, namespace)
        request = execute_request(catalog_of(items), pick(items, name).request, f"req-{name}")
        runner = executor(items, namespace)
        first = await runner.execute(request, context_for(request))
        other = ExecutionContext(fence=FENCE, now_at=6.0, payload_digest="different")
        second = await runner.execute(request, other)
        assert observed(second).observation.status is ObservationStatus.REJECTED
        assert_core_accepts([first, second], expect_retry=True)
