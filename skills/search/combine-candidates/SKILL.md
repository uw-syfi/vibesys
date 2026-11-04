---
name: combine-candidates
description: "Use when a vibesys optimize brief asks you to combine several candidates' changes into one new commit on a given base, or to merge judged winners for promotion. Applies each candidate's own change in the stated order, resolves conflicts as the brief says, runs the checks, measures the result with vibesys:measure, and hands back the commit and anything that could not be applied."
---

# Combine candidates

You build one new commit from several candidates' changes, in your own worktree, on the exact
base commit the brief names. You never write plans. The steps and the lead's side of a merge are in
[combining.md](references/combining.md).

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
   with `vibesys:measure` (`vibesys-cli submit measure <sha> --samples 5`) and compare it with the
   result branch head: `vibesys-cli compare <sha> --against <head SHA>`, and with each part.

## Never

Edit anything under `.vibesys/`, the task's checker or its benchmark. Add a shortcut that raises
a number without the work being done.

## Hand back

The commit SHA; each part you applied and each part, or piece of one, you could not apply and
why; the checks you ran with results; job ids and the `compare` output against the head and against the best
part. You do not decide whether the result is promoted: the lead does.
