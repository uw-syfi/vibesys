---
name: report-progress
description: Write a periodic progress report for a long-running multi-agent effort, so the reader can tell what changed, what to trust and what was decided for them. Use when an orchestrating lead reports on a schedule, at a stop, or when asked for status.
---

# Report progress

The reader was away. A report tells them what changed since the last one, how much to trust it,
and what you decided without them. It is not a log of what you did.

## Steps

1. Read the state, not your memory: plan files, measurement records, the agent list, git heads.
   Anything you cannot confirm from them is not reported as fact.
2. Find the last report's time. Report only what happened since, and name that window.
3. Write in this order:
   1. **Headline:** the current best result and how it moved, in a sentence a newcomer follows.
   2. **Changed:** each promotion or finding, in plain words, code name in parentheses.
   3. **In flight:** each running item, what it waits on, and when it should finish.
   4. **Decisions made for the user:** each choice, with the reason, so they can reverse it.
   5. **Open for the user:** choices you already made that they may want to reverse, and what
      only they can supply (access, credentials). Work continues meanwhile; never wait on a reply.
   6. **Self-check:** the checklist of the skill that asked for the report.
4. Cut every line the reader cannot act on or use to judge trust.

## Rules

- The main text reads at a high level: what was done, why it matters, what it gained. Keep
  worker or agent ids, commit SHAs, record ids and plan numbers out of sentences. They appear
  only as a parenthetical reference after the item, such as "(plan-004, record 14, a1b2c3d)".
- Every number is backed by a record, and says how many samples and against what base; the
  record and commit go in the parenthetical.
- A number from an earlier head is labeled with that head. Never present it as current.
- Separate verified (judged, recorded) from claimed (a worker's own number) from planned.
- State caveats that change trust: shared hosts, few samples, unrun tests, checks not made.
- An interim notice from an agent is not a result. Report a result only when it is final.
- A failure is reported with its cause and what happens next, in the same place as a win.
- If nothing changed, say so in one line and why; do not pad.
- Idle capacity is reported with its reason.
- Name work by what it does ("faster decode matrix multiplies"), never by an identifier.
