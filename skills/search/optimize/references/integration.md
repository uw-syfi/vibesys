# Integrating accepted plans

Where one measurement takes the whole machine for a long time, measuring every plan on its own is
the main cost of a run. An integration measures several plans at once: each is accepted on cheap
evidence, a combiner merges them into one candidate, and that candidate takes one check and one
measure. The combiner's side, and the bisection it runs when the check fails, is in the
`combine-candidates` skill; this page is yours.

## A plan's way through an integration

The CLI has no status for "accepted": acceptance is a comment, and the plan stays `chosen`.

| Step | Part plan | Integration plan |
| --- | --- | --- |
| A worker returns a candidate on cheap evidence | stays `chosen`; comment `accepted: <SHA> built on <parent>; evidence: <job ids or numbers>; predicts <share> of the headline (<how>)` | |
| You integrate | stays `chosen` | you create it (below), then `chosen` |
| Promoted | `won`, comment `won in plan <I> (<SHA>)` | `won` |
| Measured, refused (`within_noise`, `worse`) | `parked`, comment with the verdict and that its own share is unmeasured | `parked` or `rejected` |
| Named by a failed check | stays `chosen`, back to its worker (below) | `plan edit <I> --depends-on` the parts it still holds |

`chosen` here means a worker or a pending integration holds the plan. An accepted plan nobody
integrates is still owed work: it counts as outstanding, and it is integrated before the run
finishes.

The integration plan is the one plan you write, since it carries no new idea, only the claim
that its parts together gain what they predict:
`vibesys-cli plan create --kind integration --depends-on <a>,<b>,<c> --title "<plans a, b, c
together gain X% of the headline>" --gain-bound <composed bound>`. `plan view <I>` then lists its
parts and their statuses, and each part's `plan view` lists `I` among its dependents.
`plan list --ready` never shows an integration plan: its parts are `chosen`, not `won`, and you
start it the moment you create it. Its own dependents, the plans that build on the merged work,
become ready once it is `won`.

## When to integrate

Integrate the accepted plans when either holds, whichever comes first:

1. **Their composed predicted gain clears the task's bar**: the task's declared `min_gain` when it
   declares one, and otherwise a gain that `compare` can tell from noise at the task's declared
   samples (more than the spread of the head's own samples, from `vibesys-cli show <head>`).
2. **Three plans are accepted** and waiting, even if their composed prediction is short of the
   bar. Waiting longer makes a bisection longer and keeps the dependents of those plans blocked.

Also integrate whatever is accepted before the final report. A single accepted plan whose own
prediction clears the bar may be measured on its own instead, as any candidate is, when no other
plan is waiting to join it. In simulator-led mode ([simulator-led.md](simulator-led.md)), it
still gets an integration plan, with one part: the integration is the only step that runs a full
check and a measure. In megakernel divide-and-conquer mode ([megakernel-divide-and-conquer.md](megakernel-divide-and-conquer.md#glue)), an
accepted split plan never makes an integration alone: it waits for the first phase part.

## The composed prediction

Write it on the integration plan as a comment before the measure, with how you composed it, so
the measurement tests it rather than explains it. Each part states its predicted gain as a share
of the headline; compose them by what they change:

- Parts that remove separate pieces of time from one request compose by the time they remove,
  not by their gains: parts that cut shares `t1`, `t2` of the time leave `1 - t1 - t2` of it, a
  throughput gain of `1 / (1 - t1 - t2) - 1`.
- Parts that speed up the same work multiply: `(1 + g1)(1 + g2) - 1`.
- Parts that attack the same bottleneck do not add: count the largest, until the bottleneck moves.

A part that states no prediction leaves the integration with none; say so on the plan.

## The brief, and what you write after

Disjoint files are not enough. Two parts can merge without a conflict while one changes what the
other's code means: one plan changed what counted as a codec call, which silently broke another
plan's timing of those calls. Before you dispatch, read each part's diff for what it assumes about
code another part changes (what a function returns, what a call or counter stands for, a unit, an
order), and name each such pair in Watch for; the combiner checks each one after the merge.

Dispatch a combiner with the Integrate template of [briefs.md](briefs.md): the parts in order,
most proven first, so the longest prefix that passes holds the safest work. Budget one measure, the task's
check size `G` for every check (`--gpus G` on a task with a device pool), and checks for each
part you expect to fail: `1 + F * (ceil(log2 N) + 2)` for `F` parts at fault among `N`, as the
combiner's integrating reference counts them (five for three parts and one at fault). Say whether
it may bisect in parallel on the pool: fewer rounds of waiting, for more check jobs.

From the handoff, write on every part plan the integration plan, the candidate SHA, the check
and measure job ids, the `compare` verdict against the head, and the composed prediction beside
the measured value. One measure cannot split the gain among the parts; say that their own shares
are unmeasured rather than guessing them. A measured gain far below the prediction is a lead to
follow: measure a prefix of the chain to see which parts did not deliver.

## A failed check

The combiner bisects and names the part at fault. On that plan, comment the evidence: the failing
check's job id and log lines, the last prefix that passed, and whether the part fails alone or
only beside another part (name it). Dispatch its worker again with an Implement brief whose
Parent is its accepted commit and whose Why is that evidence; for an interaction, the brief names
the other part and its commit, and the worker builds on both. The rest of the integration goes on
without it, as the combiner hands it back.
