---
name: steering-a-run
description: How steers work (target, when, standing or once, withdraw) and how to propose one. Use before calling propose_steer or when asked how to guide, redirect or stop an agent.
---

# Steering a run

A steer is guidance in plain words that the run's agents read at their next turn. You
propose it with `propose_steer(text, target, mode)`; the operator edits, sends or dismisses
it. You cannot send it, and a proposal changes nothing until they send it.

**Target** says who reads it. Call `list_targets` first: it shows the exact role, attempt and
item ids the run holds now, and `propose_steer` refuses any other id (the refusal lists the
valid ones).

- `{kind: run}`: the next turn of any role (the default).
- `{kind: role, id}`: that role's turns. See the run-roles skill for what each kind of role does.
- `{kind: attempt, id}`: the next turn in that attempt. An attempt that has ended is refused.
- `{kind: item, id}`: the next turn of any attempt at that work item (an idea).

**Mode** says when:

- `next_turn`: the target's next turn takes it, once. The right default.
- `standing`: every turn of the target from then on, until the operator withdraws it. Use it
  for a rule that must hold ("keep the weights in bf16"), not for a one-off nudge.
- `interrupt`: offered to a turn running now when the provider takes a message mid-turn,
  otherwise the next turn takes it. Use it only when waiting for the next turn would waste
  work.

**After sending**, the run reports whether the steer reached a turn, was delivered mid-turn,
or was dropped (its target ended first). A standing steer shows as in force until withdrawn.

Write the text for the agent that reads it: one concrete instruction with its reason, no
reference to this chat. A steer cannot change how results are measured. Propose at most one or
two steers at a time, say what you chose and why (target and mode), and that the operator
decides.
