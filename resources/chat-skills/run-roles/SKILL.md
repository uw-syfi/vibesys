---
name: run-roles
description: What each agent role of a run does, so a steer or an answer names the right one. Use before choosing a target for a steer, or when asked who did what.
---

# What each role does

A run's agents have roles. A role id is the search's name and the role (for example
`<search>-implementer`); the run declares its own, so call `list_targets` for the exact ids
and use those, never one you guessed. The kinds of role a search may have:

- planner: plans the next ideas; works in no workspace. Steer it to change what gets tried.
- implementer: implements one idea in an isolated copy of the code and takes its own
  measurements. Steer it to change how an idea is built.
- judge: reviews a candidate and the evaluations the implementer took, and gives the verdict.
  Steer it to change what counts as acceptable.
- profiler: looks at a revision to inform later plans; produces no candidate.
- merger: merges candidates into the main line.
- outliner: outlines the steps of a task's plan once, at the start, when the task has none.

A role the run does not use does nothing: aim a steer at a role that works on what the
operator cares about. When unsure, steer the whole run or ask which role did the work, using
the tools to find it. Evaluation is not steered: a steer changes what agents are told, never
how a candidate is measured or accepted.
