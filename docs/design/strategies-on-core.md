# Running single, multi, evolve and issue_queue on the vs-core lifecycle

Design for issue #1953, phase 1 of roadmap #1952. This note maps the four strategies that
still run as imperative loops (`single`, `multi`, `evolve`, `issue_queue`) onto the pure
`vs-core` lifecycle that `dynamic` already uses. It changes no code. Where a statement is a
decision, it is written in the present tense as the design; where a part does not exist yet,
the status table in section 1.3 says so, and section 11 lists what needs the maintainer's
answer.

## 1. Scope, vocabulary and status

### 1.1 What is being ported

| CLI selection | Plugin id (persisted in the run manifest) | Strategy package | Legacy entry |
|---|---|---|---|
| `--outer-loop agent --inner-loop single-agent` | `single-agent` | `orchestration/single` | `orchestrate` |
| `--outer-loop profile-guided --inner-loop single-agent` | `profile-guided-single-agent` | `orchestration/single` | `orchestrate_profile_guided` |
| `--outer-loop agent --inner-loop multi-agent` (default) | `multi-agent` | `orchestration/multi` | `orchestrate` |
| `--outer-loop profile-guided --inner-loop multi-agent` | `profile-guided-multi-agent` | `orchestration/multi` | `orchestrate_profile_guided` |
| `--outer-loop evolve` | `evolve` | `orchestration/evolve` | `orchestrate` |
| `--outer-loop plain` | `plain` | `orchestration/issue_queue` | `orchestrate` |
| `--outer-loop dynamic` | `dynamic` | `orchestration/dynamic` | already on core |

The mapping is made in `entrypoints/cli/loops.py` (`_agent_policy_descriptor`,
`_plain_policy_descriptor`, `_evolve_policy_descriptor`). It does not change: every port keeps
its CLI selection, its plugin id and `config_version`, and its option names (section by
section below). A preset that differs only by options stays one strategy class with two
plugin registrations, as it is today.

### 1.2 Vocabulary

Readers who know the product but not `vs-core` need these terms.

- **Strategy**: a pure object with `decide(view) -> Proposal` (the next ordered decisions plus
  the new strategy state) and `on_event(view, event) -> state`. No clock, I/O, asyncio or
  randomness: time comes from `view.run.now_at`, and any randomness is a seeded generator
  state stored in the strategy state.
- **RunView**: the read-only projection of core state that a strategy sees (attempts,
  sessions, measurements, settlements, controls, adoption).
- **Decision**: what a strategy asks for: `RequestTurn`, `StartAttempt`, `Measure`,
  `Withdraw` (with a `Settle` disposition), `ProposeWinner`, `Stop`, `Operation`. Each has a
  `DecisionId` the strategy derives from its own state, so a replay re-proposes the same id and
  core deduplicates it by id and payload digest.
- **Attempt**: the unit of candidate work. It owns one concurrency slot, a workspace (an
  isolated child of a base revision, or the exclusive run root), a budget and a session scope,
  and it ends in exactly one **settlement** (assessments, `eligible`, `retention`, outcome).
- **Operation**: a strategy-declared request for I/O that is not a turn or a measurement
  (render a prompt, verify a revision, read evidence). An owner in a library executes it; the
  outcome returns as `OperationResult`.
- **Adoption**: copying the selected retained candidate into the project root and verifying
  it. Core runs it after `ProposeWinner` and reports `AdoptionResult`.
- **Suspension**: a turn that ends by waiting for an evaluation job. Core publishes
  `TurnSuspended`, later `ResumeAuthorized` once, and the strategy asks the resumed turn.
- **Shell**: the async host in `vs-runtime` that executes core's requests and feeds outcomes
  back as events. **Durable boundary**: one atomic commit of state plus the intents it implies.

Core events the strategies fold, with the consequence of each: `AttemptReady` (slot and
workspace held, begin turns), `TurnResult` (reply or typed failure), `TurnSuspended` and
`ResumeAuthorized` (wait and resume), `MeasurementResult` (trusted evidence ids),
`OperationResult`, `AttemptExhausted` (budget spent), `AttemptSettled`, `AdoptionResult`,
`IntentBlocked` (effect unknown), `Rejected` (decision refused), `ControlChanged` (operator
stop), `RunEnded`.

### 1.3 Status

| Part | Status | Phase |
|---|---|---|
| Core lifecycle, attempts, settlement, adoption, suspension, exclusive root, `wip` retention | built (`vs-core`) | n/a |
| Agent evaluation tool bridge, per-candidate budgets, `Waitable` replies | built (`docs/design/core-agent-evaluation-bridge.md`) | n/a |
| `dynamic` on core, its scenario, recovery-matrix, liveness, chaos and smoke tests | built | n/a |
| Shared building blocks B1 to B15 (section 3) | unbuilt; `dynamic` holds private copies | 2 |
| Strategy conformance suite and the promoted runtime shell test kit (T1, T2 in 3.1) | unbuilt; `dynamic` holds a private shell harness | 2 |
| `CaptureCommand` operation (profile-guided attribution) | unbuilt | 3 |
| Immutable hypothesis state, or the JSON-field bridge (8.4) | unbuilt | 3 |
| `single` and `profile-guided single` strategy | unbuilt | 3 |
| Issue-tracker operations and `FakeIssueTracker` contract suite | unbuilt | 3 |
| `issue_queue` strategy | unbuilt | 3 |
| `ValidateLocal` operation, stateless tool admission, `multi` strategy | unbuilt | 4 |
| Immutable population state, `ExportPatch` operation, `evolve` strategy | unbuilt | 5 |
| Removal of the legacy loop runtime and evaluation service | unbuilt | 6 |
| Limits extension for a resumed run with a larger round budget | undecided (Q3) | n/a |

### 1.4 Rules every port follows

These restate `CLAUDE.md` and `software-design` for this work; the sections below apply them.

1. `decide` and `on_event` are pure functions of strategy state, `RunView` and static config.
   Config is an input, not state: restarting with the same options reproduces every decision.
2. Strategy state holds only policy memory that core cannot know. Lifecycle and accounting
   facts (attempt phases, slot use, charges, session phases, the evidence ledger,
   settlements) are read from `RunView`, never copied. Where a copy is unavoidable (a round
   record keeps the metric rows it accepted) the copy is of a fact core already committed and
   a property test checks the two agree.
3. One external fact is one event. Live execution, recovery inspection and replay all fold
   the same event through the same code.
4. Every agent reply is parsed at the boundary into a typed model that rejects unknown keys.
   An option the run does not offer is absent from the schema.
5. Agent-bound text is rendered from templates through the render operation. Python passes
   data, never prompt text.
6. State is a tree of strict, frozen, tuple-only `Value` models, so it passes
   `validate_immutable_schema` and round-trips through the envelope codec.

## 2. What `dynamic` already does

`dynamic` is the worked example: `strategy/` holds the pure strategy, `core_policy/` holds
limits, reply schemas, projection and prompts, and `src/vibesys/dynamic_core.py` plus
`dynamic_roles.py` do the thin wiring. One workstream is one attempt.

| Step of one workstream | Decision or request | Answering event |
|---|---|---|
| verify the parent revision | `Operation(VerifyParentRevision)` | `OperationResult(ParentVerification)` |
| start | `StartAttempt(ISOLATED_CHILD, base=parent, budget)` | `AttemptReady` |
| render the role prompt | `Operation(RenderRoleArtifacts)` | `OperationResult(RenderedArtifacts)` |
| implement | `RequestTurn(paid, WRITE_CANDIDATE)` | `TurnResult`; optionally `TurnSuspended`, `ResumeAuthorized` |
| read the checkpoint | none (candidate is `turn_candidate(view, ...)`) | the view shows the retained revision |
| review | `RequestTurn(free, READ_ONLY)` on the candidate | `TurnResult` |
| measure | `Measure(accuracy -> benchmark)` | `MeasurementResult` |
| read evidence | `Operation(InterpretEvidence)` | `OperationResult(EvidenceReadings)` |
| settle | `Withdraw(attempt, Settle(...))` | `AttemptSettled` |
| select and finish | `ProposeWinner`, then `Stop(drain)` | `AdoptionResult`, `RunEnded` |

The pieces the ports reuse, by file in `src/vibesys/orchestration/dynamic/strategy/`:

- `_draft.py`: `Draft` (ordered decisions, state update, earliest wake time) and the pure
  builders `run_scope`, `attempt_scope`, `session_for`, `request_turn`, `measurement_plan`.
- `_ids.py`: the identity grammar (`component` percent-encoding, id constructors, the inverse
  of core's `operation:<decision>` identity).
- `_state.py`: `Step` (NEEDED, RENDERING, RENDERED, AWAITING, SUSPENDED, RETAINING),
  `TurnRecord` (charge, corrections, drops, backoff, yield, resume), `RunPhase`, the baseline,
  hypothesis, round and winner records.
- `_attempt_events.py`: correction, lost-turn re-ask with backoff, yield and resume, the
  held-reply-until-checkpoint rule (`RETAINING`), failure with slot refund, exhaustion.
- `_settlement.py`, `_baseline.py`, `_run.py`: assessments and eligibility, the one input
  measurement, winner choice and the SEARCHING, SELECTING, ADOPTING, STOPPING, FINISHED flow.
- `_evidence.py`, `_rows.py`, `_operations.py`: trusted-evidence filtering, decoded metric
  rows, the four operation contracts.

What `dynamic` does not need and the ports do:

1. A serial search that continues one hypothesis across rounds, with cadence-based review and
   official evaluation (provisional candidates that no framework measurement covers).
2. Run-scoped turns that write agent memory (the designer's roadmap) rather than only reply.
3. A trusted command run for profile-guided attribution, and the trusted local-validation run.
4. Roles that use a profiler diagnostics tool (core refuses every tool except `evaluation`
   today; `src/vibesys/run/core_services.py`, `_refuse_unbridged_tools`).
5. A population policy with a seeded generator, concurrent children, and admission in slot
   order.
6. An external work source (the issue board) that agents also change.

## 3. Shared building blocks

Rule for placement: generic lifecycle protocol (what a correct caller of core must do) lives
in `vs-core`; mechanism another product on `vs-runtime` would need lives in a library;
VibeSys policy that two or more strategies share lives in a package beside
`vibesys.orchestration` that never imports it (`architecture.md`); strategy-specific code stays
in its strategy package. Only blocks with two or more users are listed. "Today" says where the
code sits in `dynamic`.

### 3.1 Table

| # | Block | Today | Owner | Users | `dynamic` must |
|---|---|---|---|---|---|
| B1 | Identity grammar: `component`, `IdScheme(namespace)` for decision, invocation, session, attempt and item ids, and the inverse of `operation:<decision>` | `dynamic/strategy/_ids.py` (hard-coded `dyn`) | `vs_core.api.ids` (core defines the identity rules and the `operation:` form) | dynamic, single, multi, evolve, issue_queue | use `IdScheme("dyn")`; a test pins that every id is byte-identical to today's |
| B2 | Decision builders and `Draft[S]`: scopes, `TurnShape`, `request_turn`, `operation`, `session_for`, `workspace_for`, `due(turn)` and wake time | `_draft.py` | `vs_core.api.authoring` (pure, no policy) | all five | keep only its prompt-specific `render_request` |
| B3 | `MeasurementRecipe`: evaluator, workload and environment digests, recipe artifact, ordered stages with budgets, queue allowance, submission limit; `plan(candidate, purpose, now_at)` | `_draft.measurement_plan` and `AgentEvaluationPolicy.plan` in `vs-runtime` (two copies of one fact) | `vs_core.api.evaluation` | dynamic, single, multi, evolve, and the runtime agent bridge | delete its copy; the bridge's `AgentEvaluationPolicy` wraps the same value |
| B4 | `TurnChain[C]`: charge class, corrections, lost-turn re-ask with doubling backoff, yield, suspend, resume, `chain_length(...)` for limits. `C` is the strategy's prompt-context type, so core keeps no prompt knowledge | `TurnRecord` in `_state.py`; `_correct`, `_ask_again`, `_yielded`, `on_suspended`, `on_resume` in `_attempt_events.py`; `_planner.py` | `vs_core.api.authoring` (the protocol rules: a resume is asked once, a correction names its predecessor) | all five | replace its `TurnRecord` and handlers |
| B5 | `AttemptTrack`: attempt id and generation, `ready`, `Step`, `awaiting`, `settle_sent`, failure, stop handling, refund rule (an unadmitted attempt that fails refunds its slot, an admitted one must settle), hold-reply-until-checkpoint (`RETAINING`) | `AttemptRecord` fields and `fail`, `on_ready`, `on_exhausted`, `on_blocked`, `on_rejected`, `settle_retained` | `vs_core.api.authoring` | all five | embed an `AttemptTrack` in `AttemptRecord` |
| B6a | Evidence views: `trusted_keys`, `ledger_refs`, `turn_candidate` | `_evidence.py` | `vs_core.api.evidence_views` (pure functions of core types) | dynamic, single, multi, evolve | import them |
| B6b | Decoded readings: `MetricRow`, `PartialRow`, `AcceptedReading`, `reading_of`, `accept_readings` | `_rows.py`, `_evidence.py` | `vibesys.metrics` (shared policy sibling) | dynamic, single, multi, evolve | import them |
| B7 | `StrategyOperations(namespace, context_type)`: registrations, schema refs and runtime role mapping for render, verify, interpret and retain | `_operations.py`; `_ROLE_OF_KIND` in `dynamic_core.py` | `vs_runtime.api.core` (it owns `OperationRole` and the owners) | all five | build from the kit with namespace `dynamic`, so the persisted kind names (`dynamic.render_role_artifacts`, ...) do not change |
| B8 | Candidate lifecycle policy: (a) `settle_for` (assessments, eligibility, retention, outcome), (b) the input baseline stage, (c) the run phases SEARCHING, SELECTING, ADOPTING, STOPPING, FINISHED with `choose_winner`, `selection_of`, `on_adoption` and the three run endings | `_settlement.py`, `_baseline.py`, `_run.py` | new package `vibesys.candidates` beside `orchestration` | (a) and (b): dynamic, single, multi, evolve (evolve skips the baseline); (c): all five | move the three modules; a small `CandidateConfig` carries the knobs they read |
| B9 | Hypothesis round machine: the serial hypothesis search glue for `HypothesisSearch` (next round, plan validation and one correction, `start` with parent and rollback base, cadence, `close_round`, `best`) and the `RecordInput` builder from core facts | `HypothesisSearch` exists; the glue is in `single/orchestration.py` and `multi/orchestration.py` (two near-copies) | `vibesys.hypothesis` | single, multi | none; it already shares `cadence` |
| B10 | `Waitable[R]`: reply union of a result and `waiting_for_evaluation`, with its discriminator | `ImplementerReply`, `JudgeReply`, `WaitingForEvaluation` in `dynamic/models.py` | `vs_runtime.api.core` (the bridge parses the same wire form) | all five | import it |
| B11 | Core-path role factory and capability sets (`CONTINUATION`, `MEASURING`, the evaluation tool) | `dynamic_roles.py` | `vibesys.core_roles` (thin product wiring) | all five | call the factory |
| B12 | Core wiring: store the evaluation recipe, read the evaluation identity, derive stage budgets and flags (`EvaluationInputs`), `agent_evaluation_policy`, the workspace-mode and sandbox check, plugin registration and `RuntimeRecordProjector` | `dynamic_core.py` | `vibesys.core_wiring` (thin product wiring) | all five | shrink to a caller |
| B13 | Core-protocol prompt templates and contexts: reply correction, resume, wait-unrecorded, agent-evaluation notes, environment notes, run context | `dynamic/core_policy/prompts/*.j2`, `strategy/_prompts.py` | `vibesys.prompts` (shared templates) | all five | import them |
| B14 | `CaptureCommand` operation: run a configured argv in a revision's workspace with a timeout and return bounded output | none (`run.commands.capture_output` in legacy `attribution.py`) | contract in `vs_runtime.api.core`, owner in `vs_runtime` | single, multi (profile-guided) | none |
| B15 | Stateless tool admission: a role may name a tool server that holds no run state (profiler diagnostics) | none; `_refuse_unbridged_tools` rejects all but `evaluation` | `vs_runtime.api.core` (`AgentTool` declares its class), `vibesys.run.core_services` | multi, evolve, issue_queue | none |
| T1 | Runtime shell test kit: `ScriptedExecutors`, `run_shell`, restart-at-boundary | `tests/vibesys/orchestration/dynamic/strategy/_shell.py`, `_executors.py`, `_run.py` (about 1,000 lines) | `vs_runtime.api.testing` | all five | import it |
| T2 | Whole-loop harness and invariants: `_harness.py`, `_chaos.py`, `loop_invariants.py`, smoke e2e | `tests/composition/dynamic/`, `tests/support/loop_invariants.py`, `tests/e2e/test_dynamic_loop_smoke_e2e.py` | `tests/composition/_loop/` and `tests/support/` parametrized by plugin id | all five | become one parametrization |

### 3.2 Contracts of the blocks that carry new rules

- **B2/B4/B5 together are the strategy authoring kit.** It is pure and imports only core
  types. It is the answer to "cost per new strategy": a strategy declares its phases, its
  turn table and its settle policy, and gets correct handling of corrections, lost turns,
  suspension, checkpoints, refunds, stop and exhaustion without rewriting it. Each
  transition returns the new track and never proposes a decision; `decide` proposes, as in
  `dynamic` today.
- **B3** makes a trusted measurement and an agent's tool measurement share one identity by
  construction, which is what `core-agent-evaluation-bridge.md` already requires ("an agent
  measurement and a trusted one share one identity"). Today two functions build it.
- **B7** keeps one operation per runtime role. A strategy with its own namespace gets its own
  kinds, so two strategies never collide in one registry, while the request and outcome models
  and the runtime owners are the same code.
- **B8(a)** decides eligibility from evidence only: a candidate is winner-eligible when every
  configured gate has a trusted passed reading, review did not reject it, no failure is
  recorded, and it beats the baseline beyond noise (when a baseline was measured). A measured
  candidate whose accuracy passed is retained as a buildable parent even if its benchmark
  failed.
- **B15** narrows the existing refusal: `evaluation` is bridged, a stateless diagnostics
  server is admitted, and a server that writes run state (the issue board) stays refused until
  it is bridged or replaced (8.3).

### 3.3 Not extracted

Used by one strategy only, so they stay in that strategy: the parent catalog and portfolio
plan validation (`dynamic`), the population policy and selectors (`evolve`), issue-board
rendering and policy (`issue_queue`), the pre-round decision and local validation
(`multi`), plan-correction details of `HypothesisSearch` users.

### 3.4 Refactoring `dynamic`

The extraction is behavior-preserving and lands before any port, as separate refactor commits
(two hats). Evidence that nothing changed: the existing dynamic suite passes unchanged
(`test_scenarios`, `test_recovery_matrix`, `test_liveness`, `test_state_codec`,
`test_real_core`, `test_concurrent_shell`, the composition and chaos tests), plus one new
transcript test that runs the scenarios before and after and compares every decision id and
payload digest, because those ids are persisted in run envelopes.
