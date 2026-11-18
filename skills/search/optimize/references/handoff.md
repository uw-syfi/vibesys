# A handoff: the lead's design for the workers to build

A handoff is the lead's written answer to "what should be built next, and why", kept while the
optimization runs. Use one when the task builds a system from nothing or redesigns one, and a
domain skill offers a model to explore designs with before building them (`serving-studio`'s
simulator for LLM serving). Without such a model, plans from planners are enough; do not keep a
handoff for its own sake.

You keep it, and only you: it is how the one agent that sees the whole run tells each worker
what its part is. Planners and profilers feed it; workers build one item of it at a time.

## What it holds

A directory beside the worktrees, outside the repository (`vibesys:state`):
`<parent>/<name>.worktrees/handoff/<task>/`, with one subdirectory per version and one status file.

```
handoff/<task>/
  STATUS.md        one line per item you briefed, newest last; yours to append to
  v1/
    README.md      the design in one sentence, then the design; the evidence it rests on
    CONTRACT.md    what a measurement must match to be compared (from OBJECTIVE.md), how to measure
    ITEMS.md       the items, in the order to build them
    data/          the model's outputs the items cite (result directories, exported tables)
  v2/ ...
```

The domain skill says what goes in each file for its domain (`serving-studio`:
`references/handoff.md`). Each item in `ITEMS.md` has:

- **Build:** one thing a worker can finish, commit and measure in a pass or two ("the decode
  attention kernel for these shapes", "replicas behind one router").
- **Why:** the evidence: the model's time share, a profile, a measurement.
- **Reference numbers:** what the model says the item should show, when it models the item well;
  leave the line out when it does not. For a kernel the evidence names (the kernel that dominates
  the profile), naming the kernel and its share is the item; no predicted gain is needed.
- **Done when:** what a worker observes when the item works (it serves, a check passes, a
  measured number moves), in the task's own terms.

Reference numbers are guidance, never a gate: a measured result that misses them is recorded and
weighed, and the next item can still go ahead. Never change the workload, a metric or the
contract to close a gap.

## Versions

A version is frozen once you brief its first item. When the evidence changes the design (a
measurement contradicts the model, the model itself changes, a better design shows up), write the
next version as a new directory, with a first line in its `README.md` saying what changed and
why. Earlier versions stay as they were, so every brief still points at what it was given.

## The loop

1. **Explore.** Ask the domain skill's model your questions: load it yourself for a few targeted
   runs, or dispatch a profile brief for a sweep. Write version 1.
2. **Brief an item.** Each item you fund becomes one plan, which you create from the item:
   `plan create --title "<the item, as a claim>" --body-file <the item's text> --workstream <n>`,
   all items of a handoff in one workstream named after it. Dispatch an implement worker with
   the `Handoff:` line of [briefs.md](briefs.md). Items with no order between them and disjoint
   files can run at once.
3. **Read the outcome.** After the measure and the judge, append one line to `STATUS.md`:
   `<date> v<k> item <id> plan <n>: <commit or none>, <measured value or why none>, <gap or "as referenced">`.
   Name a miss as one of:
   - **setup difference:** the measurement did not match the contract;
   - **implementation gap:** the setup matches, the build falls short of its reference;
   - **model gap:** the model is wrong here, shown by a measurement it does not reproduce;
   - **design invalidated:** the evidence ranks another design higher.
4. **Decide.** Go on to the next item, brief the same one again with what was learned, open an
   item that extends the model (below), or write the next version. Every decision goes into the
   plan as a comment, as for any plan.

Planners keep running beside the handoff, for ideas it does not cover; name the handoff in their
brief so they read it first. On resume, read `STATUS.md` and the newest version before the plan
list. Every progress report says, in a line, where the handoff stands: its version, the items
done, the one in progress, and the largest open gap.

## Extending the model

When the model cannot describe the task (the domain skill found nothing to simulate), or a model
gap blocks a design decision, an item may extend the model: profile what it lacks on the task's
hardware, or add what it does not model. Brief it with the tool-extension variant of
[briefs.md](briefs.md): the worker works in its own copy of the tool, never in the one the run
uses, and hands back a commit of the tool. Point the next version at that commit; predictions of
earlier versions keep naming the revision they came from. Send the extension upstream only when
the user's instructions say so.
