---
name: plan-hypotheses
description: "Use when a vibesys optimize brief asks you to propose one hypothesis as a plan. Read-only on code: browses the plans, the saved results and the code, picks its own idea, bounds the possible gain from measured peaks and observed numbers, and stores one falsifiable proposal with vibesys-cli plan create. Changes no code and runs no benchmark."
---

# Plan a hypothesis

You propose one idea; the lead decides. You edit no code, run no benchmark and submit no job. You
write only your own plan. Your brief gives the task name, the CLI context flags and maybe one
short nudge. Nothing else: finding the idea is your job. The CLI assigns the plan number. Plans
and where things live: `vibesys:state`.

**Which CLI.** Where this skill writes `vibesys-cli`, run `"$VIBESYS_CLI"`, never the one on
`PATH`: your shell's `PATH` may hold another build than the one this run means to use. A run
started by the orchestrator sets it. If it is unset or empty, stop and say `VIBESYS_CLI is not
set; export it as the absolute path of the vibesys-cli to use`.

## Nudge

A nudge is a way of looking, never a target: find the bottlenecks (profile first, attack the
largest measured cost); close the gap to the speed of light the most effective way; redesign from
first principles; remove work (computation, copies, launches that need not happen); stack what
exists (combine parked or won plans); mine prior art (the domain skill, published state of the
art) and apply an idea directly or combine several that fit this task's specialization; reduce
variance and tails. With no nudge, look however the
evidence suggests.

## Bold ideas

The nudge "bold" asks for a high-risk, high-reward idea: one that costs a lot to build, may
replace how the work is done today, and pays off a lot if it works. Out-of-the-box is the point,
so do not trim it to the next small step, and do not drop it for low confidence. Two things stay
binding. The upside is still capped by the headroom you computed. And you plan it in stages:
the first step is a cheap feasibility check with a stated kill criterion (the number below which
you stop), so a miss costs one worker pass. Fill the Bold fields in
[proposal.md](references/proposal.md).

## Work

1. Read `OBJECTIVE.md`; its rules bind every idea. Read the plan index first
   (`vibesys-cli plan list --json`) and open full plans only for plans near your idea
   (`plan view <n>`). Then browse the saved results (`vibesys-cli result`, `show <sha>`, `jobs`,
   `logs <job>`) and the code. A profile an earlier agent kept lives in its plan's comments.
   Read the code of the revision you plan on (usually the result branch head) without checking it
   out: `git show <sha>:<path>`, `git grep <pattern> <sha>`, `git diff <a> <b>`. The checkout you
   start in belongs to the user or the lead; never change its index, HEAD or files.
2. **Bound first.** Compute the speed-of-light floor of the part you look at from the bytes and
   operations it must move and the hardware's measured peaks. The gap to the observed time is the
   headroom. Use the benchmark's observed operating point, not its configured one. An idea may not
   claim more than that headroom; if the part's time is unmeasured, say "unattributed" and make the
   first step a measurement. Bound the whole system too: the most the task's fixed resources allow
   end to end, including whatever drives the load. Say which resource sets that ceiling and where
   the current result sits under it. A part's headroom is real only below that ceiling. When your
   ceiling for an objective differs from the one `vibesys-cli bound list` shows, declare yours with
   `bound declare` and say how you derived it; the earlier one stays in the history.
3. **Claim early.** As soon as you chose an idea, run `vibesys-cli plan create --title "<claim>"
   --body-file -` with a stub body (one line of mechanism) and `--gain-bound <share>`, the most
   the idea could gain as a share of the headline (see step 5); note the number it prints. Before
   investing in details, list the plans again: if a sibling already claimed the same mechanism,
   pick another idea and change yours with `plan edit <n> --title ... --body-file -`.
4. Check premises cheaply: read the installed library or code to confirm the API, kernel or flag
   you rely on exists and fits this hardware. Check the idea against the rules in `OBJECTIVE.md`.
5. Write the proposal as the plan's body, as in [proposal.md](references/proposal.md): one claim
   that can succeed or fail alone, the arithmetic, the files touched, and the smallest first step
   that reaches a measurable commit in one worker pass. Leave the status `open`. Decisions and
   results are the lead's comments, not yours.
6. **State the bound.** The lead decides whether to go on or to stop from the bounds of the plans
   that are still open or parked, and a tool reads them, so state yours as a number and not only
   in prose: the most the idea could gain, as a share of the headline, from the arithmetic of
   step 2 (the headroom of the part divided by the headline, capped by the system's ceiling).
   Round up, never down. Set it with `plan edit <n> --gain-bound 5%` (it replaces the old one;
   `--gain-bound-of headline` is the default and the only reference). "Unattributed" is not a
   number: bound the idea by the whole system's ceiling, or make its first step the measurement
   and say so. The headline is `vibesys-cli result` (before a first promotion, the base's value
   from `vibesys-cli show <base>`). When the headline has moved a lot since you wrote the bound,
   state it again.

## Hand back

The plan number, the gain bound you stated, your confidence stated plainly, what you could not
establish, and the measurement that would most change your view. Derive the idea from evidence and first principles;
do not copy code from other systems.
