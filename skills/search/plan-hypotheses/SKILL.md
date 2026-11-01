---
name: plan-hypotheses
description: "Use when a vibesys optimize brief asks you to propose one hypothesis as a plan file. Read-only on code: browses the optimization state and the code, picks its own idea, bounds the possible gain from measured peaks and observed numbers, and writes one falsifiable proposal into plans/plan-NNN-<slug>.md. Changes no code and runs no benchmark."
---

# Plan a hypothesis

You propose one idea; the lead decides. You edit no code, run no benchmark and submit no job. You
write only your own plan file. Your brief gives the task name, a plan number NNN and maybe one
short nudge. Nothing else: finding the idea is your job. State layout and plan files:
`vibesys:state`.

## Nudge

A nudge is a way of looking, never a target: find the bottlenecks (profile first, attack the
largest measured cost); close the gap to the speed of light the most effective way; redesign from
first principles; remove work (computation, copies, launches that need not happen); stack what
exists (combine parked or won plans); reduce variance and tails. With no nudge, look however the
evidence suggests.

## Work

1. Read `OBJECTIVE.md`; its rules bind every idea. Read the plan index first
   (`grep -H -m3 -E '^(status|claim|gain):' plans/*.md`) and open full plan files only for plans
   near your idea. Then browse `measurements/`, `scratch/` profiles and the code.
2. **Bound first.** Compute the speed-of-light floor of the part you look at from the bytes and
   operations it must move and the hardware's measured peaks. The gap to the observed time is the
   headroom. Use the benchmark's observed operating point, not its configured one. An idea may not
   claim more than that headroom; if the part's time is unmeasured, say "unattributed" and make the
   first step a measurement.
3. **Claim early.** As soon as you chose an idea, create `plans/plan-NNN-<slug>.md` with the
   header and a stub (slug and one-line claim). Before investing in details, list `plans/` again:
   if a sibling already claimed the same mechanism, pick another idea and rename your file.
4. Check premises cheaply: read the installed library or code to confirm the API, kernel or flag
   you rely on exists and fits this hardware. Check the idea against the rules in `OBJECTIVE.md`.
5. Fill the `## Proposal` section as in [proposal.md](references/proposal.md): one claim that can
   succeed or fail alone, the arithmetic, the files touched, and the smallest first step that
   reaches a measurable commit in one worker pass. Leave `## Decision` and `## Result` empty.

## Hand back

The plan file path, your confidence stated plainly, what you could not establish, and the
measurement that would most change your view. Derive the idea from evidence and first principles;
do not copy code from other systems.
