# Roadmap

You (the Orchestrator) own this file end-to-end. Update it every round
*before* deciding the round's task. The framework names this file in
your next prompt but does not inject or parse its contents, so inspect it
with tools and format it however you find useful. Follow these conventions
so the structure stays legible:

- **Major** items: structural changes expected to move the headline
  performance metric meaningfully. Derive them from measured bottlenecks and
  the objective rather than from examples supplied by the framework. Usually
  1-3 rounds each.
- **Minor** items: bug fixes, polish, gates (correctness recoveries,
  tiny kernel swaps, accuracy bumps). Usually 1 round each.
- Use one of these four statuses, and note rounds spent on each
  in-progress item:
  - `todo` — not started.
  - `in_progress` — actively being worked on this round (or recent rounds).
  - `done` — implemented, profiler-verified, hitting (close to) predicted impact.
  - `parked` — implementation is buggy or incomplete, but you believe the
    *direction* is sound. Returnable to `in_progress` later. Use this when
    the metric isn't moving for an *implementation* reason rather than a
    workload reason.
  - `abandoned` — the *direction* itself doesn't fit this workload. Strict
    requirement (see below) before flipping to this state.
- For each item include a one-line *why* (predicted impact, what
  bottleneck it addresses).

If any Major item is `todo` or `in_progress`, this round's task should
serve it. Do NOT drop into Minor work while a Major sits unfinished
unless that Minor is genuinely blocking the Major (state the dependency
explicitly when you do).

## `parked` vs `abandoned` — get this distinction right

These two are not the same thing and the loop's behavior degrades if you
treat them as one bucket:

- **`parked`** is the right call when (a) you predicted the change would help,
  (b) the implementation satisfies correctness gates, but (c) the headline
  metric did not move because the intended path did not activate or the
  implementation is incomplete. The direction itself remains believable.
  Mark it `parked`, move to a different Major, and return when you have a
  concrete debugging hypothesis or other measured avenues are exhausted.

- **`abandoned`** is the right call only when the *direction itself* is the
  wrong fit for this workload. It requires a mechanism-level autopsy explaining
  why the change cannot help here, not merely that a few measurements were
  flat. If you cannot write that mechanism, use `parked` instead.

**Hard rule for `abandoned` autopsies:** name a code-level, system-level, or
hardware-level mechanism—not a behavioral observation. A flat performance
number alone is not a mechanism. If activation evidence is absent, treat that
as a debugging task and use `parked` with a concrete hypothesis.

## Major

(populate on round 1 based on the objective)

## Minor

(none yet)

## Done

(none yet)

## Parked

(none yet)

## Abandoned

(none yet)
