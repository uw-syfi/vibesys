# A base that fails the check

A base that fails the check can never be measured, and `vibesys-cli promote` refuses every
candidate `unmeasured` while the commit to beat has no measurement. The task is not at fault when
the base is simply missing the code the task checks, as in a task that asks for an
implementation written from nothing. Do not edit the task, measure the base, or work around the
refusal. Bootstrap instead: the first passing implementation becomes the base.

## Tell it from a broken task

Read the failed check's log (`vibesys-cli logs <job>`) and `OBJECTIVE.md`.

- The check fails because the candidate code is missing or incomplete (the module or file the
  task imports does not exist, an assertion fails on behavior), and `OBJECTIVE.md` or the
  repository shows there is no implementation yet: a from-scratch base. Go on below.
- The check fails for a reason in the task (the checker itself crashes, a data file or dependency
  it needs is absent, a path it names does not exist, the manifest is wrong): a broken task. Stop,
  change nothing and report, as `vibesys:optimize` says.
- Unsure: treat it as broken. A wrong bootstrap costs a worker's whole budget.

## Bootstrap

1. Do not create the result branch yet, and do not measure the base. Choose the run id and the
   CLI context line as for any start. Create one plan, titled
   "bootstrap: the smallest correct implementation that passes the check", and comment the base
   SHA and the failing check job.
2. Dispatch one implement worker for it, with this in the brief: the pass criterion is
   `vibesys-cli submit check <sha> --wait` passing on a full commit (`Checks: pass`,
   `vibesys:commit`); the budget is check jobs only, no measure; the implementation is the
   simplest correct one, with no stub, no special case for the check's inputs, no change under
   `.vibesys/`, and every rule of `OBJECTIVE.md` obeyed. Speed is a later round's work.
3. Dispatch a judge for the result, and say in its brief that this is a bootstrap with no speed
   claim, so correctness and honesty decide its verdict.
4. When the judge passes it and `git diff --stat <base> <bootstrap SHA> -- .vibesys` prints
   nothing, create the result branch at the bootstrap commit, once
   (`git branch vibesys/result/<task> <bootstrap SHA>`). Creating a branch that does not exist
   moves nothing; nothing was promoted. The bootstrap commit is now the base: comment it on the
   plan, close the plan, and go on with step 4 of Start (measure the base) and the Loop. The first
   base commit stays unmeasured and is never promoted.
5. If two bootstrap attempts in a row end without a passing commit, stop and report the blocker
   the worker named.

On a resume with no result branch and an open bootstrap plan, resume the bootstrap; do not start
a second one.
