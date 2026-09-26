---
name: review-pr
description: Review VibeSys pull requests and local diffs for correctness, regressions, architecture violations, insufficient tests, contract or documentation drift, and risky prompt changes. Use when a user asks to review, audit, assess, or find bugs in a VibeSys PR, branch, commit, patch, or working-tree change, including pre-merge reviews and second-pass reviews after updates.
---

# Review PR

## Overview

Produce a high-signal review of the change, not a general code tour. Prioritize
specific defects introduced by the diff and verify each finding against the
repository's contracts, architecture, tests, and affected callers.

## Workflow

1. Resolve the review target and intent.
   - Read `AGENTS.md`, load the `software-design` skill and, when the diff
     touches tests, the `testing` skill, and read every applicable subtree
     instruction before judging the change. Read
     `docs/contributing/coding-best-practices.md` when the diff touches size
     limits or lint waivers.
   - Inspect repository status before switching branches or fetching a PR.
     Preserve user changes and prefer reviewing without mutating the worktree.
   - Read the PR title, body, linked issue, base/head commits, changed files,
     existing review threads, and check results when available. Use the local
     branch or merge-base diff when reviewing unpublished work.
   - Derive the intended behavior and correctness properties from the PR
     body and the linked issue. Record each concrete claim (what changes,
     what is fixed, what stays the same, what was tested) so the summary can
     check it against the diff.
   - Call out material ambiguity instead of silently choosing an
     interpretation.
   - Do not submit a GitHub review, comment, approval, or change request unless
     the user explicitly asks for that external write.

2. Build a change map before evaluating individual lines.
   - Classify changed files by owner: framework, reusable library, client,
     evaluator, example bundle, prompt/template/skill, configuration/schema,
     generated artifact, test, or documentation.
   - Read enough surrounding implementation and history to understand the
     existing pattern. Trace changed interfaces to their producers, consumers,
     serialization boundaries, and tests with `rg`.
   - Identify authoritative sources and generated outputs. Review generated
     diffs as behavior, but request fixes in the source of truth.
   - Look for coupled artifacts that should have changed but did not: tests,
     snapshots, schemas, generated types, package data, examples, contracts,
     migration or compatibility code, and user-facing documentation.
   - Reconcile the claims with the code. For each claim from step 1, find
     the diff hunks that implement it. Note claims with no implementing
     change, changes the body does not mention, a fix that addresses a
     different cause than the issue describes, a "no behavior change" claim
     next to a behavior change, and tests that are named but absent.

3. Apply the review guide.
   - Read [references/review-guide.md](references/review-guide.md) completely.
   - Use only the language and surface sections relevant to the diff, plus the
     cross-cutting checks.
   - Review new code placement explicitly. Favor an existing canonical owner;
     extract a standalone reusable capability into `libs/` only when it has a
     real interface and reuse boundary. Flag both misplaced shared behavior and
     abstractions that add indirection without protecting a boundary.

   - Run a design pass for any diff that adds or moves modules, interfaces,
     dependencies, data flow, or config. Check the PR's `Design` section
     against the diff: is the named owner right, is the stated interface the
     one the diff exposes, does the direction of dependencies match, and is
     every new `tach.toml` edge declared and justified. Scan the diff for the
     red flags in `.agents/skills/software-design/references/red-flags.md`.
     Look for an existing violating pattern that the diff extends, and for a
     new or split module that does not declare its public interface.
   - Then read across files, as an LLM reviewer, for problems linters cannot
     see: one concept duplicated in several files, shallow wrappers that add a
     layer without a new abstraction, callers reaching into a module's
     internals, and an interface that some implementations only half support.
     Treat these as findings only when they carry a material maintenance or
     correctness consequence.

4. Reproduce the bug when the PR is a bug fix.
   - Whenever practical, demonstrate the defect at the merge base and its
     absence at the PR head. Use a temporary worktree
     (`git worktree add <scratch>/pr-<N> <ref>`) so the user's checkout is
     never mutated; remove it when done.
   - Choose the narrowest deterministic reproduction: run the PR's own new
     tests against the base source (they must fail there and pass at the
     head), write a throwaway test or script from the issue's steps, or for
     TUI rendering fixes render the affected view at a fixed width with the
     `clients/tui` test harness. Drive the live TUI through the
     `tui-bug-hunt` harness only when no cheaper reproduction exists and the
     cost is justified.
   - Report the reproduction as evidence: the command, the observed failure
     at the base, and the observed result at the head. If a reproduction was
     not possible, say why and treat the fix as unverified rather than
     confirmed. A fix whose new tests also pass at the merge base has not
     been demonstrated.

5. Verify candidate findings.
   - Confirm that the reviewed change introduced or exposed the problem.
   - State the concrete input, state, platform, or execution path that triggers
     it and the observable consequence.
   - Check nearby tests and run the narrowest useful read-only test or
     reproduction when practical. Never claim a command ran when it did not.
   - Reject findings based only on taste, hypothetical future needs, or a
     preference already enforced automatically unless there is a material
     correctness or maintenance consequence.
   - Re-read the final diff and each cited line. Distinguish a proven defect
     from a residual risk that needs more evidence.

6. Report the review.
   - Always start with a summary of the PR: two to five sentences on what
     the diff actually does, in terms of behavior and the modules it touches,
     followed by a `Discrepancies` line. List every gap between what the PR
     body or linked issue claims and what the source code does: unmentioned
     changes, unimplemented claims, a different root cause, a wider or
     narrower scope than the issue, or verification claims the diff does not
     support. Write `Discrepancies: none` when the claims and the code
     agree. A discrepancy that changes correctness or merge safety also
     becomes a finding below.
   - Then list findings ordered by severity. Use `[P0]` through `[P3]`, a short
     actionable title, one compact explanation, and the tightest changed-line
     range that demonstrates the issue.
   - Explain why the behavior is wrong and when it occurs; include a fix
     direction only when it clarifies the required contract. Do not write a
     patch unless the user also asks for implementation.
   - Keep summaries brief. After findings, report the reproduction outcome
     for bug fixes, then list assumptions or open questions, checks run, and
     residual risks only when they add information.
   - If there are no actionable findings, say so plainly and mention meaningful
     verification gaps. Do not invent low-value findings to populate a review.

## Severity

- `P0`: Release-blocking or broadly catastrophic; immediate action is required.
- `P1`: A serious, likely defect that should block merge.
- `P2`: A real defect with limited scope or a meaningful missing safeguard.
- `P3`: A small but concrete correctness or maintainability problem worth fixing.

Severity reflects impact, likelihood, and blast radius rather than diff size.

## Review Boundaries

- Treat tests, docs, prompts, templates, skills, schemas, and manifests as
  product behavior when users or agents depend on them.
- Focus on changed behavior. Mention pre-existing defects separately and only
  when they materially affect whether this change is safe.
- Do not expose credentials, private logs, or sensitive paths in review output.
- Do not approve merely because checks pass; tests demonstrate selected
  properties, not the absence of regressions.
