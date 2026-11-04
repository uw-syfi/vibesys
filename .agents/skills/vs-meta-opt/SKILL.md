---
name: vs-meta-opt
description: Run and audit long-lived VibeSys meta-optimization campaigns that improve VibeSys as an optimizer rather than directly optimizing its bespoke candidate. Use when Codex should launch or resume VibeSys with a specified input, monitor every completed round, use independent counterfactual reviews when a trajectory plateaus or becomes uncertain, diagnose ineffective agent-system behavior, implement and commit VibeSys changes, compare later trajectory windows, and repeat within round, time, cost, or intervention budgets.
metadata:
  kind: workflow
---

# VS Meta Opt

## Purpose

Operate a feedback loop that judges and improves the optimizer, not the bespoke system it produces. Run VibeSys, observe whether its search is effective, change VibeSys when evidence supports a meta-hypothesis, resume the campaign under the new version, and determine whether the trajectory improved.

Preserve a strict boundary:

- Treat candidate implementation choices and domain performance techniques as inner-loop work.
- Treat role behavior, search policy, prompts, skill availability, evidence, evaluation, context, lifecycle, and cost control as meta-optimization surfaces.
- Do not become a shadow candidate designer.

## Read Only What Is Relevant

- Read [run-control.md](references/run-control.md) before launching, monitoring, pausing, resuming, or terminating a long-running VibeSys campaign.
- Read [effectiveness-rubric.md](references/effectiveness-rubric.md) to audit a round, trajectory window, or multiple campaigns.
- Read [counterfactual-review.md](references/counterfactual-review.md) before using independent subagents to assess whether VibeSys is choosing strong next steps.
- Read [diagnosis-and-levers.md](references/diagnosis-and-levers.md) to attribute a deficiency, choose a VibeSys control lever, or check for prompt leakage.
- Read [meta-experiments-and-commits.md](references/meta-experiments-and-commits.md) before changing VibeSys or validating whether a system change helped.
- Use [meta-run-pr-template.md](assets/meta-run-pr-template.md) to open and maintain the required draft PR.

Read campaign state, history, prompts, diffs, logs, profiles, timings, costs, and evaluations from artifact paths. Read deltas after the initial inspection; do not repeatedly inject durable history or unchanged output into context.

## Run Contract

Before starting, record in the draft PR:

- Exact VibeSys command, specified input paths and revisions, environment, and initial VibeSys commit.
- Campaign identity, initial checkpoint, and correctness/evaluation contract.
- Maximum rounds, wall-clock time, accelerator time or cost, agent budget when applicable, and meta-interventions.
- A terminal reserve for final official evaluation, evidence publication, and cleanup.
- Initial trusted frontier and available effectiveness measurements.

Treat one VS Meta Opt run as one bounded meta-optimization campaign. It owns one branch and one draft PR, but may contain multiple VibeSys process segments, sequential meta-hypotheses, clean commits, validations, and explicit reverts.

## Control Loop

### 1. Open the draft PR

Start from the intended VibeSys parent on a dedicated clean branch. Open the draft PR before changing behavior and initialize [meta-run-pr-template.md](assets/meta-run-pr-template.md). Use its description as the living effectiveness ledger.

### 2. Launch or resume VibeSys

Run VibeSys with the specified input and exact recorded command. Preserve the campaign checkpoint and candidate state across process restarts. Record the VibeSys commit governing each campaign segment.

Use the simple edit path by default: finish the current round, stop or pause VibeSys, edit and commit in the same checkout, then resume. When overlapping work is worthwhile, optionally develop in a separate clean worktree while leaving the active framework checkout unchanged. Treat every framework change as inactive until a recorded campaign segment starts or resumes under its commit.

### 3. Monitor with adaptive heartbeats

Choose the next sleep interval from the current phase, expected duration, progress signal, failure risk, and budget. Check sooner during startup, near a deadline, or after a warning; sleep longer during healthy work that is expected to take time. Avoid busy polling.

After each sleep, inspect only new status, events, summaries, or a bounded log tail. If nothing changed and the process is healthy, do not rerun semantic analysis or reinsert unchanged output.

Perform at least one trajectory check after every completed VibeSys round. Between round completions, use heartbeats only to verify liveness, phase progress, deadlines, and resource safety.

### 4. Check the trajectory after every round

Use a lightweight per-round check to answer:

- Did the round produce valid new evidence or frontier progress?
- Did the next decision respond to the evidence available?
- Is VibeSys repeating a known failure, tuning within noise, or losing context?
- Did evaluation, coordination, or infrastructure consume disproportionate work?
- Are correctness, provenance, and reward-hacking defenses intact?
- Does enough budget remain for the current path or another meta-experiment?

If the trajectory remains healthy, record a compact checkpoint and continue without proposing a change. Trigger a deeper audit when a warning recurs, the trajectory plateaus, integrity is uncertain, cost rises unexpectedly, or the current validation window ends.

When the next-step quality is uncertain—especially during a plateau—consider a counterfactual trajectory review using [counterfactual-review.md](references/counterfactual-review.md). Give fresh subagents the legitimate objective and raw artifact paths, but withhold VibeSys's proposed next step until they produce independent alternatives. Do not run this panel automatically after every round.

### 5. Audit and form the next meta-hypothesis

Apply [effectiveness-rubric.md](references/effectiveness-rubric.md) at round, window, and campaign timescales. Classify findings, including `Other` when nothing fits cleanly, and identify the narrowest VibeSys-owned mechanism supported by evidence.

When a counterfactual review exists, compare its proposals with VibeSys's choice using evidence fit, causal model, expected impact, falsifiability, cost, integrity, and novelty. Do not assume the subagents are right. If a stronger alternative exposes a proposal gap, explain why VibeSys missed it using the failure taxonomy before changing anything.

Propose one next system intervention with expected meta-metric effects, likely regressions, a validation window, success criteria, and a reversion condition. Improve how VibeSys generates or evaluates directions; do not paste the stronger candidate proposal into an always-on prompt. If evidence is insufficient, improve instrumentation before changing behavior.

### 6. Pause, change, validate, and commit

Stop or pause at a durable round boundary. Keep candidate code and campaign artifacts separate from the VibeSys change. Implement the narrowest coherent design that fully addresses the meta-hypothesis; scope by causal responsibility, not line count.

Follow repository conventions and architecture boundaries. Reuse or extend existing abstractions when they own the behavior, and introduce or refactor abstractions when that materially improves ownership, data flow, reuse, testability, or ergonomics. Include adjacent cleanup required for a natural design, but exclude unrelated refactoring. Reject one-off conditionals, compatibility shims, and other patchwork used only to minimize the diff.

Run targeted checks, perform the leakage review, and make a clean rationale-bearing commit.

When the intervention changes a schema for VibeSys-generated code or persisted run state, snapshot the existing state, update the canonical schema and all producers and consumers, migrate the live campaign once, validate the result, and resume using only the new representation. Commit the schema, one-way migration, and tests atomically. Do not retain parallel runtime schema implementations.

Keep prompts procedural and neutral. Do not encode a candidate optimization, known bottleneck, benchmark-specific trick, prior winning implementation, or hidden evaluator behavior directly into prompts. Adding or improving modular, versioned, selectively loaded skills is allowed and must be recorded as a capability change.

Update the PR intervention log and remaining budget immediately.

### 7. Resume and measure the effect

Resume the same campaign from the recorded checkpoint under the new VibeSys commit. Record the new segment boundary. Observe the predeclared number of rounds or other validation condition before attributing an effect, unless correctness, safety, or decisive contrary evidence requires stopping early.

Use framework-owned evidence to retain, revise, or explicitly revert the intervention. Do not certify it merely because its author expected it to work.

### 8. Repeat within budget

Continue monitoring, per-round trajectory checks, meta-hypotheses, commits, and validation until the budget reaches its terminal reserve or another stopping condition fires. Do not begin an intervention without enough remaining budget to evaluate it.

Finish with the terminal official evaluation when enabled, publish final evidence, terminate owned processes and remote resources, update the PR disposition, and leave the worktree clean.

## Token-Efficient Monitoring Rules

- Let the agent choose sleep intervals contextually; do not impose one global cadence.
- Never skip the trajectory check after a completed round.
- Track the last event or log cursor, round, phase, progress time, VibeSys commit, remaining budget, and next wake reason.
- Read append-only deltas and compact summaries before raw logs.
- Keep complete logs on disk and load only evidence needed for the current decision.
- Separate cheap heartbeat/liveness checks from expensive semantic audits.
- Use counterfactual subagents only when their expected information value justifies their token and time budget.
- Treat silence according to phase-specific expectations; do not call healthy long work a hang.
- Capture bounded diagnostics before terminating a stalled process, then clean up every owned resource.

## Guardrails

- Do not propose first-order candidate optimizations as the audit result.
- Do not put discovered optimizations or tricks into always-on agent prompts.
- Do not judge VibeSys only by the final candidate score.
- Do not force every finding into a closed taxonomy; use `Other` with rationale.
- Do not infer causality from one successful round without considering opportunity, cost, and confounders.
- Do not mix a VibeSys system intervention with candidate changes in one commit.
- Do not optimize for the fewest changed lines at the expense of an ergonomic, project-conventional design.
- Do not mutate the active framework checkout; stop first or use an optional separate worktree. Generated code and state may be migrated at a safe boundary to one new canonical schema.
- Do not spend the terminal reserve on a change that cannot be evaluated.

## Expected Handoff

Return:

1. Run command, input, campaign checkpoint, active VibeSys commit, and evidence paths.
2. Per-round and window effectiveness findings, including `Other` when applicable.
3. Current phase, last progress, next wake reason, and remaining budgets.
4. Limiting VibeSys mechanism and the next intervention with leakage assessment.
5. Commit and campaign-segment log with validation dispositions.
6. Final effectiveness assessment, terminal evaluation, cleanup status, and unresolved confounders.
