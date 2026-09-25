<!-- PR title: <type>(<optional scope>): <imperative summary>, e.g. "fix(ui): keep the cursor in view after resize". Types: feat, fix, docs, refactor, perf, test, build, ci, chore. -->

## Problem

<!--
Motivate the change. Explain why we are doing this in the first place, what
user or maintainer pain it addresses, and link any relevant issues.
-->

## Solution

<!--
Describe the high-level design. Call out important implementation choices,
tradeoffs, boundaries, and any behavior that reviewers should inspect closely.
-->

### Design

<!--
Answer the software-design checkpoint (.agents/skills/software-design/):
- Owner: which module or package owns this change, and why there.
- Interface: the public interface added or changed, and who depends on it.
- Direction: which way data and dependencies flow, and any new coupling or
  `tach.toml` edges (name each one and why it is needed).
- Drift: known violations you left untouched, extended, or filed as an issue.
Write "n/a: <reason>" for a question that does not apply.
-->

### Architecture

<!--
Describe the ownership model and major components involved in the solution.
For nontrivial control flow or cross-boundary changes, include a Mermaid diagram
or equivalent sketch that shows how the pieces interact.
-->

## Verification

<!--
Summarize how you checked the change. Include automated tests, manual checks,
benchmarks, or reasons a particular check was not run.
-->

### Correctness properties

<!--
List the invariants, contracts, or expected behaviors this PR preserves or
introduces, especially for user-facing behavior and shared interfaces.
-->

### Testing

<!--
List the exact commands or workflows run, plus the relevant result.
-->
