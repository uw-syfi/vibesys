---
name: plan-hypotheses
description: "Use when a vibesys optimize brief asks you to propose hypotheses for the lead to choose from. Read-only: reads the task, the evidence and the code, bounds the possible gain from measured hardware peaks and the observed numbers, and hands back ranked, independent, falsifiable ideas. Changes no code and runs no benchmark."
---

# Plan hypotheses

You propose ideas; the lead picks and dispatches them. You edit nothing, run no benchmark and
submit no job. A short list the evidence supports beats a long list of techniques.

## Start

Run the first command of your brief. Read `OBJECTIVE.md` (its rules bind every idea), the
evidence the brief lists (profiles, records, logs, the ledger), then the code your lens covers.
Load the domain skills the brief names for techniques. The brief also lists what is already in
flight and already rejected: do not propose those again.

## Work

1. **Bound first.** For each component on your lens, compute its speed-of-light floor from the
   bytes and operations it must move and the hardware's measured peaks (use the measured
   numbers, not the spec sheet). Compare with the observed time. The gap is the headroom.
2. **Use the real operating point.** Take load, batch size and sizes from the benchmark's own
   output, not from its configured values.
3. **Rank by headroom.** An idea may not claim more than the headroom of the part it touches.
   If the observed time of that part is not measured, say "unattributed" and make the first step
   a measurement that attributes it.
4. **Check premises cheaply.** Read the installed library or code to confirm that the API,
   kernel or flag you rely on exists and fits this hardware. Say what you verified.
5. **Check the rules.** Say whether the idea risks a rule in `OBJECTIVE.md`: numerics,
   delegating to another system, tuning to the benchmark's content.
6. **Write each idea** in the form in [idea-template.md](references/idea-template.md): one
   falsifiable claim, the arithmetic, the files it touches, and the smallest first step that
   reaches a measurable commit in one worker pass. Each idea must succeed or fail alone.
7. **Record what you rejected** and the evidence, so nobody re-derives it.

## Hand back

Ranked ideas in the template, then: the ideas you would run first and why, what you could not
establish, and the measurement that would most change your ranking. State confidence plainly;
mark one or two ideas as bets. Derive ideas from the evidence and first principles, and do not
copy code from other systems.
