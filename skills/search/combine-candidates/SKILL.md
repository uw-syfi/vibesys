---
name: combine-candidates
description: "Use when a vibesys optimize brief asks you to combine several candidates' changes into one new commit on a given base, to merge judged winners for promotion, or to integrate several accepted plans into one measured candidate. Applies each candidate's own change in the stated order, resolves conflicts as the brief says, runs the checks, measures the result with vibesys:measure, bisects a failed check down to the plan at fault, and hands back the commit and anything that could not be applied."
---

# Combine candidates

You build one new commit from several candidates' changes, in your own worktree, on the exact
base commit the brief names. You never write plans. The steps and the lead's side of a merge are in
[combining.md](references/combining.md). A brief that names an integration plan and its parts is
an integration: follow [Integrate](#integrate) as well.

**Which CLI.** Where this skill writes `vibesys-cli`, run `"$VIBESYS_CLI"`, never the one on
`PATH`: your shell's `PATH` may hold another build than the one this run means to use. A run
started by the orchestrator sets it. If it is unset or empty, stop and say `VIBESYS_CLI is not
set; export it as the absolute path of the vibesys-cli to use`.

## Start

Run the first command of your brief and confirm the worktree and branch. Read `OBJECTIVE.md`.
Load `vibesys:commit`, `vibesys:measure` and the domain skills the brief names.

## Work

1. Apply each candidate's own change in the order given. Its own change is the diff from the
   commit it was built on to the candidate, not from the head:
   `git diff <built-on> <candidate> | git apply --3way`
2. A hunk that does not apply is left with conflict markers: resolve them as the brief says,
   so each candidate's change survives. Keep every part's behavior. Do not redesign a part.
3. If an instruction cannot be followed without redesigning a candidate, stop and say which
   instruction and why. Main stays as it is.
4. Run the project's fast tests and fix what the combination broke.
5. Commit as `vibesys:commit` says: a full commit with `Checks: pass`. Without a measurement
   budget, return the SHA as `ready for measurement` for the lead. Otherwise measure the commit
   with `vibesys:measure` (`vibesys-cli submit measure <sha> --plan <n>`, the plan
   your brief names) and compare it with the
   result branch head: `vibesys-cli compare <sha> --against <head SHA>`, and with each part.

## Integrate

An integration brief names an integration plan and the plans it merges, each accepted on cheap
evidence and not yet measured. Its steps replace Work steps 1, 4 and 5, and are in
[integrating.md](references/integrating.md):

1. Apply the parts one at a time and make a full commit after each, so each prefix of the list
   is a commit: the chain the bisection walks.
2. Check the top once, at the task's check size (`--gpus <G>` on a task with a device pool,
   the same on every check and repeat), and measure it once, each linked to the integration plan
   and, by repeating the submission, to every part (the repeats run nothing).
3. On a failed check, bisect over the prefixes with checks only, `ceil(log2 N)` of them for N
   parts, or several prefixes a round where the pool allows and the brief says so; say whether the part named fails alone or only beside another; then finish without it
   from the longest prefix that passed, within the brief's check budget.

## Never

Edit anything under `.vibesys/`, the task's checker or its benchmark. Add a shortcut that raises
a number without the work being done.

## Hand back

The commit SHA; each part you applied and each part, or piece of one, you could not apply and
why; the checks you ran with results; job ids and the `compare` output against the head and
against the best part; for an integration, the chain and the bisection as integrating.md says. You do not decide whether the result is promoted: the lead does.
