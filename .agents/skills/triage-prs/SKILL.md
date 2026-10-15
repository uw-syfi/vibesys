---
name: triage-prs
description: Triage the open pull requests of the VibeSys repository. Inventory them, classify each by readiness (CI, merge conflicts, draft state, review state, stack position, PR template compliance, staleness), decide whether the author or a reviewer acts next, optionally apply routine labels such as bug, and report a prioritized maintainer queue. Use when a user asks to triage, prioritize, sort, summarize, or check the status of open PRs, find what is ready to merge or review, what is stale or blocked, what needs author action, or to label open PRs.
metadata:
  kind: workflow
---

# Triage PRs

## Overview

Produce a maintainer's action queue for the open pull requests, not a review
of each one. Classify from GitHub signals and PR bodies, then delegate deep
code review of individual PRs to the `review-pr` skill when the user selects
them.

## Workflow

1. Resolve scope and write permissions.
   - Default to a read-only report. Labels, comments, review requests,
     closing, readiness changes, and project fields are writes and need an
     explicit user request. "Tag bug fixes with bug" or "label feature PRs"
     is such a request.
   - Confirm the repository with `gh repo view --json nameWithOwner`. Do not
     switch branches or fetch PR heads; triage never needs a checkout.
   - Restrict to a subset (author, label, path, PR list) when the user names
     one.

2. Collect signals.
   - Run `scripts/pr_inventory.sh` from this skill directory. It prints one
     JSON object per open PR with CI, mergeability, review, stack, template,
     and closing-issue signals. Field meanings are in
     [references/triage-signals.md](references/triage-signals.md).
   - Re-query PRs whose `mergeable` is `UNKNOWN` after a short wait. GitHub
     computes mergeability lazily.
   - For PRs whose bucket depends on review content, read the latest
     maintainer comment or review with `gh pr view N --comments`. Findings in
     this repository are often plain comments with `[P0]` to `[P3]` prefixes.
   - Read the `Problem` section of each PR body. Do not read diffs except to
     resolve a classification question, such as whether a change is a bug fix
     or a refactor.

3. Classify.
   - Assign exactly one bucket per PR using the precedence in
     [references/triage-signals.md](references/triage-signals.md):
     `blocked`, `needs-author`, `draft`, `stale`, `ready-to-merge`,
     `needs-review`.
   - Record the concrete blocker: failing check names, conflict, unanswered
     findings, missing template section, open predecessor PR.
   - Decide who acts next. Trust `waiting_on` unless the latest comment shows
     otherwise.

4. Run the cross-PR checks: overlapping files between open PRs, shared
   commits, stacks that are claimed in the body but based on trunk, split
   series and their order, superseded work, and large PRs with no review.

5. Estimate review effort per PR from the logical change, not the diff
   size. Use `size`, `large_files`, and `bulk_lines` from the inventory and
   the rules in the reference. A mechanical rename, a regenerated schema, a
   recorded fixture, or a vendored source tree inflates the diff without
   adding review work; say so in the report.

6. Apply requested writes.
   - Labeling: follow the label rules in the reference. Ground `bug` in the
     PR's `Problem` section or a closing issue labeled `bug`; ground
     `enhancement` in a `Solution` that adds capability or a surface, and
     `refactor` in a stated absence of behavior change. Skip borderline cases
     and list them for the user.
   - Before any comment, close, or review request, restate exactly which PRs
     are affected. Never close or mark ready without a per-PR instruction.
   - Re-read each edited PR with `gh pr view N --json labels` and report the
     result.

7. Report using the template in the reference. Lead with the maintainer's
   ordered actions, then the queue table, cross-PR notes, and writes made.
   End with the recommended review sequence: small targeted PRs first, then
   old PRs that need a close or follow-up decision, then stacks bottom-up,
   then larger work ordered by what it unblocks, with drafts and blocked PRs
   as a watch list. State anything not verified after the sequence.

## Boundaries

- Triage judges readiness and ownership, not code quality. Say "needs
  review" rather than guessing whether the code is correct.
- Do not count passing checks as evidence of correctness; the queue tells
  the maintainer where to spend review time.
- Report unverified state (transient `UNKNOWN` mergeability, missing token
  scopes for the project board, CI that has not run) instead of filling it
  in.
- Do not expose credentials, private logs, or sensitive paths.
