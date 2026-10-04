"""A new workstream builds on content that passed accuracy only if that content is reproducible.

An agent-submitted evaluation records the digest of the content it checked.
The framework offers that revision as a parent only while the revision still
exports to the same digest; a plan naming one that does not is corrected with
the field named, never given the base revision instead.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from tests.support.evaluation_scenarios import ScenarioOutcome, ScenarioSpec, build_scenario
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    dynamic_options,
    portfolio,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, ORCHESTRATOR
from vibesys.orchestration.dynamic.parents.api import ParentCatalog
from vs_evaluation.api import EvidenceKind
from vs_evaluator_protocol.api import PartialMeasurement, Progress
from vs_runtime.api import (
    AgentCapability,
    AgentEvaluation,
    AgentEvaluationStatus,
    RunFacts,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole
    from vs_runtime.api.testing import TurnResponder


def _blocked(identifier: str) -> dict[str, object]:
    return {"summary": f"Stopped {identifier}.", "outcome": "blocked", "evidence": []}


def _child(parent: str, identifier: str = "b", revision: str | None = None) -> dict[str, object]:
    plan = portfolio(identifier)
    workstreams = plan["workstreams"]
    assert isinstance(workstreams, list)
    (child,) = workstreams
    selection = {"parent_hypothesis_id": parent}
    if revision is not None:
        selection["parent_revision"] = revision
    return {**plan, "workstreams": [{**child, **selection}]}


async def _accuracy_passed(revision: str, patch: str) -> AgentEvaluation:
    with TemporaryDirectory(prefix="parent-evidence-") as directory:
        async with build_scenario(
            Path(directory),
            ScenarioSpec(
                revision=revision,
                patch=patch,
                kinds=(EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK),
                outcome=ScenarioOutcome.CORRECTNESS_FAIL,
                benchmark_failure=True,
                failure="benchmark too slow",
            ),
        ) as scenario:
            return scenario.projection


def _run(tmp_path: Path, script: Script, *, digest_matches: bool) -> tuple[FakeRun, str]:
    """Run ``a`` (its turn submits an accuracy pass of its start revision), then the plans."""
    holder: list[FakeRun] = []
    evaluated: list[str] = []

    async def respond(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id == IMPLEMENTER.id and not evaluated:
            run = holder[0]
            workspace = run.workspaces.candidates[-1]
            revision = workspace.revision
            assert revision is not None
            evaluated.append(revision)
            content = f"patch for {revision}" if digest_matches else "other content"
            run.evaluation.record_agent_evaluation(
                workspace, await _accuracy_passed(revision, content)
            )
        return script.respond(role, history, message, response)

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", accuracy_configured=True),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        holder.append(run)
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_rounds=2))
        return run

    run = asyncio.run(scenario())
    return run, evaluated[0]


def _messages(script: Script, role_id: str) -> list[str]:
    return [message for role, _, message in script.calls if role == role_id]


@pytest.mark.parametrize("content", ["reproduces", "changed"])
def test_an_agent_verified_candidate_is_a_parent_only_while_its_content_reproduces(
    tmp_path: Path, content: str
) -> None:
    digest_matches = content == "reproduces"
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("a"), _child("a"), portfolio("b")],
            IMPLEMENTER.id: [_blocked("a"), _blocked("b")],
        }
    )

    run, evaluated = _run(tmp_path, script, digest_matches=digest_matches)

    planner = _messages(script, ORCHESTRATOR.id)
    implementer = _messages(script, IMPLEMENTER.id)
    if digest_matches:
        assert len(planner) == 2
        assert f'"hypothesis_id":"a","title":"Investigate a","revision":"{evaluated}"' in planner[1]
        assert f"Parent revision: `{evaluated}`" in implementer[1]
    else:
        assert len(planner) == 3
        assert "Buildable candidates" not in planner[1]
        assert (
            "workstreams[0].parent_hypothesis_id: 'a' cannot be built on (its revision no "
            "longer holds the content that passed accuracy)"
        ) in planner[2]
        assert f"Parent revision: `{evaluated}`" not in implementer[1]
        assert any(
            "buildable candidate a withheld" in call.message for call in run.observations.calls
        )


async def _partial_passed(revision: str, value: float, completed: int) -> AgentEvaluation:
    """Produce trusted accuracy and failed benchmark receipts through their owner."""
    with TemporaryDirectory(prefix="parent-partial-") as directory:
        async with build_scenario(
            Path(directory),
            ScenarioSpec(
                revision=revision,
                patch=f"patch for {revision}",
                outcome=ScenarioOutcome.CORRECTNESS_FAIL,
                benchmark_failure=True,
                failure="benchmark stopped before the required rounds",
                partial=PartialMeasurement(
                    name="decode_throughput",
                    value=value,
                    direction="max",
                    unit="tokens/s",
                    target=79.7,
                    progress=Progress(completed=completed, required=72, unit="rounds"),
                ),
            ),
        ) as scenario:
            return scenario.projection


@pytest.mark.parametrize("explicit_revision", [False, True], ids=["retention", "exact-sibling"])
def test_same_turn_regression_preserves_both_verified_partial_parents(
    tmp_path: Path, *, explicit_revision: bool
) -> None:
    """The 79.835 partial remains selectable after a later 72.564 partial."""
    holder: list[FakeRun] = []
    submitted: list[AgentEvaluation] = []
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("a")],
            IMPLEMENTER.id: [_blocked("a"), _blocked("b")],
        }
    )

    async def respond(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id == IMPLEMENTER.id and not submitted:
            run = holder[0]
            workspace = run.workspaces.candidates[-1]
            # Script requester admission independently from receipt delivery.
            for ordinal, value, completed in ((1, 79.835, 71), (2, 72.564, 65)):
                revision = await workspace.snapshot("accuracy-verified intermediate")
                evaluation = (await _partial_passed(revision, value, completed)).model_copy(
                    update={"submission_index": ordinal}
                )
                submitted.append(evaluation)
                run.evaluation.record_agent_evaluation(workspace, evaluation)
        elif role.id == ORCHESTRATOR.id and submitted:
            script.calls.append((role.id, None, message))
            plan = _child("a")
            if explicit_revision:
                workstreams = plan["workstreams"]
                assert isinstance(workstreams, list)
                (child,) = workstreams
                plan["workstreams"] = [{**child, "parent_revision": submitted[0].revision}]
            return plan
        return script.respond(role, history, message, response)

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve.",
                accuracy_configured=True,
                benchmark_configured=True,
            ),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        holder.append(run)
        run.evaluation.script_root_benchmark(INPUT_BASELINE)
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_rounds=2))
        return run

    run = asyncio.run(scenario())
    planner = _messages(script, ORCHESTRATOR.id)[1]
    # Runs unchanged at origin/main: its single verified slot loses the first partial.
    for evaluation in submitted:
        assert f'"revision":"{evaluation.revision}"' in planner
        assert evaluation.content_digest is not None
        assert evaluation.content_digest in planner
    assert "79.835" in planner
    assert "72.564" in planner
    if explicit_revision:
        assert f"Parent revision: `{submitted[0].revision}`" in _messages(script, IMPLEMENTER.id)[1]
        _check_parent_prompt("parent_regression_offer", planner)
    assert run.workspaces.root.revision not in {row.revision for row in submitted}
    assert not run.workspaces.root.restore_calls
    _assert_regression_catalog(run, planner, submitted)


def _assert_regression_catalog(
    run: FakeRun, planner: str, submitted: list[AgentEvaluation]
) -> None:
    rows = json.loads(next(line for line in planner.splitlines() if line.startswith("[{")))
    by_revision = {row["revision"]: row for row in rows}
    first, latest = (by_revision[item.revision] for item in submitted)
    assert first["best_partial"] is True
    assert first["latest_verified"] is False
    assert latest["best_partial"] is False
    assert latest["latest_verified"] is True
    committed = run.state.commits[-1].value
    restored = type(committed).model_validate_json(committed.model_dump_json())
    assert restored == committed
    state = restored.model_dump(mode="json")
    snapshots = _parent_ledger(run).model_dump(mode="json")["snapshots"]
    assert {row["revision"] for row in snapshots} == {row.revision for row in submitted}
    for evaluation in submitted:
        offered = by_revision[evaluation.revision]
        assert offered["handle_id"] == evaluation.handle_id
        assert offered["content_digest"] == evaluation.content_digest
    assert state["winner_revision"] is None
    assert state["adoption_pending"] is False
    assert state["baseline"]["metric_value"] == 1.0
    assert state["baseline"]["partial_measurement"] is None


async def _assert_sibling_isolation(
    run: FakeRun,
    evidence: AgentEvaluation,
    source_revision: str,
    source_index: int = 0,
    invocation_sequence: int = 1,
) -> None:
    source = run.workspaces.candidates[source_index]
    child = run.workspaces.candidates[-1]
    assert child.id != source.id
    assert source.revision == source_revision
    assert source.restore_calls == []
    assert source.agent_restore_calls == []
    state = run.state.commits[-1].value.model_dump(mode="json")
    producer = next(row for row in state["workstreams"] if row["hypothesis_id"] == "a")
    assert producer["budget"]["spent"] == 1
    assert producer["invocation_sequence"] == invocation_sequence
    assert producer["phase"] == "implementing"
    await child.snapshot("independent sibling edits")
    assert source.revision == source_revision
    assert await run.workspaces.export_patch(evidence.revision) == f"patch for {evidence.revision}"


@pytest.mark.parametrize("invalid_choice", ["revision", "continuation"])
def test_active_verified_snapshot_refills_isolated_siblings_and_corrects_invalid_choices(
    tmp_path: Path, invalid_choice: str
) -> None:
    """Active receipts survive refill without resuming or resetting their producer."""

    async def scenario() -> tuple[FakeRun, list[str], AgentEvaluation]:
        verified = asyncio.Event()
        source_release = asyncio.Event()
        planner_messages: list[str] = []
        holder: list[AgentEvaluation] = []
        children_started: list[str] = []
        planning_turn = 0
        failures: list[str] = []

        async def respond(
            role: AgentRole,
            _history: tuple[str, ...],
            message: str,
            _response: type[BaseModel] | None,
        ) -> object:
            nonlocal planning_turn
            if role.id == ORCHESTRATOR.id:
                planner_messages.append(message)
                planning_turn += 1
                if planning_turn == 1:
                    return portfolio("a", "filler")
                await verified.wait()
                assert f'"revision":"{holder[0].revision}"' in message
                if planning_turn == 2:
                    return (
                        portfolio("a", continue_hypothesis=True)
                        if invalid_choice == "continuation"
                        else _child("a", revision="unknown-revision")
                    )
                identifier = "paging" if not children_started else "scheduler"
                return _child("a", identifier, holder[0].revision)
            assert role.id == IMPLEMENTER.id
            if "Implement and verify a." in message:
                source = run.workspaces.candidates[0]
                revision = await source.snapshot("verified producer intermediate")
                holder.append(
                    (await _partial_passed(revision, 79.835, 71)).model_copy(
                        update={"submission_index": 1}
                    )
                )
                run.evaluation.record_agent_evaluation(source, holder[0])
                verified.set()
                await source_release.wait()
                await source.snapshot("producer continues independently")
                assert await run.workspaces.export_patch(revision) == f"patch for {revision}"
                return _blocked("a")
            if "Implement and verify filler." in message:
                await verified.wait()
                return _blocked("filler")
            identifier = "paging" if "Implement and verify paging." in message else "scheduler"
            children_started.append(identifier)
            try:
                assert f"Parent revision: `{holder[0].revision}`" in message
                await _assert_sibling_isolation(run, holder[0], holder[0].revision)
            except Exception as error:
                failures.append(str(error))
                source_release.set()
                raise
            finally:
                if identifier == "scheduler":
                    source_release.set()
            return _blocked(identifier)

        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", accuracy_configured=True),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=2, max_rounds=2))
        assert failures == []
        assert children_started == ["paging", "scheduler"]
        return run, planner_messages, holder[0]

    run, planner, evidence = asyncio.run(scenario())
    _assert_active_result(run, planner, evidence, invalid_choice)


def _assert_active_result(
    run: FakeRun, planner: list[str], evidence: AgentEvaluation, invalid_choice: str
) -> None:
    assert len(run.workspaces.candidates) == 4
    assert evidence.revision in planner[1]
    assert evidence.revision in planner[2]
    if invalid_choice == "revision":
        assert "unknown-revision" in planner[2]
    else:
        assert "still in flight" in planner[2]
    _check_parent_prompt(f"parent_active_{invalid_choice}_correction", planner[2])
    state = run.state.commits[-1].value.model_dump(mode="json")
    children = [
        row for row in state["workstreams"] if row["hypothesis_id"] in {"paging", "scheduler"}
    ]
    assert [row["parent_revision"] for row in children] == [evidence.revision] * 2
    assert state["winner_revision"] is None
    assert state["adoption_pending"] is False
    sessions = [session for session in run.agents.sessions if session.role.id == IMPLEMENTER.id]
    assert {session.member_id for session in sessions} == {"a", "filler", "paging", "scheduler"}
    assert len({session.workspace.id for session in sessions}) == 4


def _check_parent_prompt(name: str, prompt: str) -> None:
    path = Path(__file__).with_name("fixtures") / "prompts" / f"{name}.txt"
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        path.write_text(prompt, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == prompt


async def _accuracy_with_running_benchmark(revision: str) -> AgentEvaluation:
    with TemporaryDirectory(prefix="parent-pending-") as directory:
        async with build_scenario(
            Path(directory),
            ScenarioSpec(revision=revision, patch=f"patch for {revision}", pending_benchmark=True),
        ) as scenario:
            assert scenario.projection.status is AgentEvaluationStatus.PENDING
            assert [stage.kind for stage in scenario.projection.stages] == ["accuracy"]
            return scenario.projection


def _continued_parent() -> dict[str, object]:
    plan = portfolio("a", continue_hypothesis=True)
    entries = plan["workstreams"]
    assert isinstance(entries, list)
    (item,) = entries
    plan["workstreams"] = [{**item, "task": "Continue and verify a."}]
    return plan


class _ContinuationParents:
    def __init__(self, tmp_path: Path) -> None:
        self.verified = asyncio.Event()
        self.release = asyncio.Event()
        self.capture: list[AgentEvaluation] = []
        self.calls: list[str] = []
        self.failures: list[str] = []
        self.planning = 0
        self.offer: str | None = None
        self.run = _parent_run(tmp_path, self.respond)

    async def respond(
        self,
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == ORCHESTRATOR.id:
            return self.plan(message)
        assert role.id == IMPLEMENTER.id
        return await self.implement(message)

    def plan(self, message: str) -> dict[str, object]:
        self.planning += 1
        if self.planning == 1:
            return portfolio("a", "seed")
        if self.planning == 2:
            return _continued_parent()
        if len(self.calls) < 2:
            if not self.calls:
                self.offer = message
            assert f'"revision":"{self.capture[0].revision}"' in message
            return _child(
                "a", "paging" if not self.calls else "scheduler", self.capture[0].revision
            )
        return portfolio("done")

    async def implement(self, message: str) -> dict[str, object]:
        if "Implement and verify a." in message:
            return _blocked("initial a")
        if "Continue and verify a." in message:
            return await self.produce()
        if "Implement and verify seed." in message:
            await self.verified.wait()
            return _blocked("seed")
        if "Implement and verify done." in message:
            return _blocked("done")
        return await self.child(message)

    async def produce(self) -> dict[str, object]:
        source = self.run.workspaces.candidates[-1]
        revision = await source.snapshot("active continuation accuracy settled")
        self.capture.append(
            (await _accuracy_with_running_benchmark(revision)).model_copy(
                update={"submission_index": 1}
            )
        )
        self.run.evaluation.record_agent_evaluation(source, self.capture[0])
        self.verified.set()
        await self.release.wait()
        await source.snapshot("continued producer changes")
        return _blocked("continued a")

    async def child(self, message: str) -> dict[str, object]:
        identifier = "paging" if not self.calls else "scheduler"
        self.calls.append(identifier)
        try:
            assert f"Parent revision: `{self.capture[0].revision}`" in message
            await _assert_sibling_isolation(
                self.run, self.capture[0], self.capture[0].revision, 2, 2
            )
        except Exception as error:
            self.failures.append(str(error))
            self.release.set()
            raise
        finally:
            if identifier == "scheduler":
                self.release.set()
        return _blocked(identifier)


def test_active_continuation_accuracy_receipt_is_buildable_before_benchmark_finishes(
    tmp_path: Path,
) -> None:
    """Refill consumes a retained accuracy receipt while its producer is resumed."""

    async def scenario() -> FakeRun:
        scenario = _ContinuationParents(tmp_path)
        await PLUGIN.orchestrate(scenario.run, dynamic_options(max_in_flight=2, max_rounds=3))
        assert scenario.failures == []
        assert scenario.calls == ["paging", "scheduler"]
        assert scenario.offer is not None
        _check_parent_prompt("parent_active_continuation_accuracy_offer", scenario.offer)
        return scenario.run

    run = asyncio.run(scenario())
    state = run.state.commits[-1].value.model_dump(mode="json")
    (snapshot,) = _parent_ledger(run).model_dump(mode="json")["snapshots"]
    assert snapshot["accuracy"]["outcome"] == "passed"
    assert snapshot["benchmark"] is None
    children = [
        row for row in state["workstreams"] if row["hypothesis_id"] in {"paging", "scheduler"}
    ]
    assert [row["parent_revision"] for row in children] == [snapshot["revision"]] * 2
    assert state["winner_revision"] is None
    assert state["adoption_pending"] is False


def _parent_run(tmp_path: Path, respond: TurnResponder) -> FakeRun:
    return FakeRun(
        PLUGIN,
        project_root=tmp_path,
        facts=RunFacts(domain_id="generic", objective="Improve.", accuracy_configured=True),
        responder=respond,
        supported_extra_tools={"evaluation", "profiler"},
        supports_parallel_candidates=True,
        supported_agent_capabilities={
            AgentCapability.MCP_SERVERS,
            AgentCapability.SESSION_REUSE,
            AgentCapability.PROVIDER_SESSION_RESUME,
            AgentCapability.DURABLE_TURN_CONTINUATION,
        },
    )


def test_offered_snapshot_changed_during_planning_requires_a_fresh_correction(
    tmp_path: Path,
) -> None:
    """An accepted planner reply cannot materialize content invalidated after the offer."""

    async def scenario() -> tuple[FakeRun, list[str], str]:
        messages: list[str] = []
        capture: list[AgentEvaluation] = []

        async def respond(
            role: AgentRole,
            _history: tuple[str, ...],
            message: str,
            _response: type[BaseModel] | None,
        ) -> object:
            if role.id == ORCHESTRATOR.id:
                messages.append(message)
                if len(messages) == 1:
                    return portfolio("a")
                if len(messages) == 2:
                    run.workspaces.set_patch(capture[0].revision, "content changed after offer")
                    return _child("a", "rejected", capture[0].revision)
                return portfolio("corrected")
            assert role.id == IMPLEMENTER.id
            if not capture:
                workspace = run.workspaces.candidates[-1]
                revision = await workspace.snapshot("verified parent")
                capture.append(await _partial_passed(revision, 79.835, 71))
                run.evaluation.record_agent_evaluation(workspace, capture[0])
            return _blocked("candidate")

        run = _parent_run(tmp_path, respond)
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_rounds=2))
        return run, messages, capture[0].revision

    run, messages, revision = asyncio.run(scenario())
    assert len(messages) == 3
    assert revision in messages[1]
    assert "parent" in messages[2]
    assert "Correction required" in messages[2]
    assert "Base parent option" in messages[2]
    state = run.state.commits[-1].value.model_dump(mode="json")
    assert [row["hypothesis_id"] for row in state["workstreams"]] == ["a", "corrected"]
    assert len(run.workspaces.candidates) == 2
    assert state["workstreams"][1]["parent_revision"] != revision


def _parent_ledger(run: FakeRun) -> ParentCatalog:

    catalog = run.state.namespace("dynamic-parents").load_optional("catalog.json", ParentCatalog)
    assert catalog is not None
    assert ParentCatalog.model_validate_json(catalog.model_dump_json()) == catalog
    return catalog
