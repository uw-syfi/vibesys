# Integrating accepted plans

An integration merges several plans that were each accepted on cheap evidence (CPU tests, an
on-device self-test, a microbenchmark, a passing `submit check`) into one candidate, and pays for
one check and one measure of it instead of one of each per plan. Your brief names the integration
plan, `I` below, and its parts `P1`..`PN` in the order to apply them, each at its accepted commit
and the commit it was built on.

## Build a chain of prefixes

Apply the parts one at a time on the head, and commit after each, so that every prefix of the
list is a commit of its own:

```
head = C0 -> C1 (head + P1) -> C2 (head + P1 + P2) -> ... -> CN (all parts)
```

1. For each part in order: `git diff <built-on> <accepted SHA> | git apply --3way`, resolve any
   conflict markers as [combining.md](combining.md) says (shared files keep every part's change),
   run the project's fast tests, and make a full commit (`vibesys:commit`) such as
   `feat(integrate): apply plan 12, fused rmsnorm` with `Checks: pass` and `Plan: <I>`.
2. When the fast tests fail at `Ck`, the combination broke there, and that cost no device job:
   fix the resolution if the conflict was yours to resolve; otherwise leave `Pk` out, go on with
   the next part on `C(k-1)`, and hand back `Pk` as not applied, with the failing test.
3. Write down which files each part touched and every hunk you resolved by hand: a failure in a
   hand-resolved hunk is the combination's fault before it is a part's.

The prefixes cost nothing to keep and are what the bisection checks, so never squash them. The
top of the chain is the candidate.

## Check and measure once, linked to every plan

On a task that needs GPUs or other devices from a pool, every `submit check` below takes
`--gpus <G>`, the task's check size: the fewest devices its check runs on, which your brief gives
(`vibesys:measure`). Use the same `G` on every check of the integration. A check is saved for the
device count it ran on, so a repeat meant to reuse a check must give the same `G`; another count
is a new check. A task with no device pool takes no `--gpus`.

Check the top, `CN`, once, linked to the integration plan, and wait for it:
`vibesys-cli submit check <CN> --gpus <G> --plan <I> --wait`. On a pass, link the check to every
part by submitting it again once per part (`submit check <CN> --gpus <G> --plan <Pi>`): a saved
pass is reused, so each repeat prints the same job, runs nothing, and the part's `plan view` lists
it. Never repeat a check that failed: a failure saves no result, so a repeat runs the check again.

Then measure it, on the pass the check gave: a measure takes the whole node and no `--gpus`.
Measure the same way: `vibesys-cli submit measure <CN> --samples <S> --plan <I>`, where `S`
is the task's declared samples (your brief gives it), then the same command with `--plan <Pi>`
for each part, which joins the job in flight or reuses its result, then `vibesys-cli wait <job>`
and `vibesys-cli compare <CN> --against <head SHA>`. A repeat that prints a new job id means a
flag differed: cancel it (`vibesys-cli cancel <job>`).

## Bisect a failed check

A check that fails on `CN` and passed on the head (`C0`) changed from pass to fail somewhere in
the chain. Before you bisect, read the log (`vibesys-cli logs <job>`): a job that failed for a
reason outside the code (a timeout of the placement, a lost host) is not a failed check; say so
and stop. Otherwise bisect over the prefixes, with checks only, never a measure:

```
lo = 0 (passes: the head)      hi = N (fails: the top)
while hi - lo > 1:
    mid = (lo + hi) // 2
    vibesys-cli submit check <C_mid> --gpus <G> --plan <I> --wait
    pass -> lo = mid      fail -> hi = mid
the part at fault is P_hi; C_lo is the longest prefix that passes
```

Each bisection check is linked to `I` only. The lead quotes the failing check's job id on the
part named, as its evidence.

This takes `ceil(log2 N)` checks: one for two parts, two for three or four, three for up to
eight. It needs no new commit, since each prefix exists. When it ends, `C_lo` has a passing check
(or is the head), which the next step builds on.

### In parallel, where the pool allows

Written above, the bisection is one check at a time: the fewest device jobs, in `ceil(log2 N)`
rounds of waiting. When the pool holds several checks at once, you may check several prefixes in
one round instead: submit them all without `--wait`, then `vibesys-cli wait` on every job. All of
`C1`..`C(N-1)` at once names the part in one round with `N - 1` checks; the two midpoints of a
three-way split take two checks a round and `ceil(log3 N)` rounds. That is fewer rounds for more
jobs, so do it only when waiting costs more than the extra jobs: a long check on an idle pool.
A smaller `--gpus` than the task's check size lets more checks fit in the pool, but only if the
check is valid at that size; such a check is a different check, so its pass is not reused at
`G`, and the candidate still needs its own check at `G`. Still checks only, never a measure.

### One part at fault, or two together

The bisection names the first part whose addition breaks the check. That part fails on its own,
or only beside an earlier part. Tell which before you hand it back:

- `hi = 1`: `C1` is the head plus `P1` alone, so `P1` fails on its own. No check needed.
- Otherwise, if `P_hi`'s accepted commit was built on the current head, check it
  (`submit check <its SHA> --gpus <G> --plan <P_hi> --wait`; a saved pass at `G` is reused for
  free). Built on
  another commit, apply its diff alone on the head in a scratch commit and check that.
- A fail: the part is at fault alone. A pass: the failure needs `P_hi` and some of `P1`..`P(hi-1)`.
  That is an interaction; report both. To name the partner, bisect again over prefixes of
  `P1`..`P(hi-1)` with `P_hi` applied on top (`head + P1..Pj + P_hi`, new commits), smallest `j`
  that fails. Spend those checks only within your budget; otherwise report the interaction with
  the candidates `P1`..`P(hi-1)`.
- When `P_hi` came in through a hunk you resolved by hand, compare that hunk with the part's own
  diff first: a resolution that dropped a line is your fix, not the part's.

A check that is not deterministic defeats any bisection. When the part named touches nothing on
the failing path in the log, check `C_hi` once more before you blame it.

### Carry on without it

Leave the part at fault out and finish the integration without it: on `C_lo`, apply
`P(hi+1)`..`PN` as before, check the new top once, and measure it once. If that check fails, bisect
the new chain the same way, from `C_lo`. When the part at fault was the last one, `C(N-1)` is the
candidate and already passed. Stop when your check budget runs out and hand back the longest
prefix that passed.

### The check budget

Every integration checks its top once. Each part at fault then costs at most `ceil(log2 N) + 2`
more checks: the bisection, the check of the part alone (none when it is `P1`), and the check of
the rebuilt top (none when it was the last part). With `F` parts at fault, at most
`1 + F * (ceil(log2 N) + 2)`: one for a clean integration, five for three parts with one at
fault. Finding an interaction's partner, and the parallel rounds above, cost more than this, and
only within the brief's budget.

## Hand back

Add to the combine handoff: the chain (each prefix SHA and the part it added), the candidate SHA
and the parts it holds, the check and measure job ids, and the `compare` output. For a failed
check: every bisection check (SHA, pass or fail, job id), the part named, whether it fails alone
or with which part, the failing check's job id and the lines of its log that show the failure,
and any part you left out. You write no plan; the lead records the outcome on each.
