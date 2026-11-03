# VibeSys orchestrator

`VIBESYS_RUN_ID` is set, so VibeSys hosts the run: it starts every agent, gives
each the tools below through the `vibesys-orchestrator` tool server, and sets `VIBESYS_AGENT_ID`,
`VIBESYS_RUN_ID` and `VIBESYS_SESSION` for it. Use these tools for every subagent. Your own
harness's subagent tools would start agents the run cannot see, stop, or count.

## Dispatch a subagent with a fresh context

Call `spawn_agent` with `role`, `brief` and optionally `plan_number`, `harness`, `model` and
`effort`. The roles are `planner`, `worker`, `judge`, `combiner` and `profiler`; use the one that
matches the skill you want the agent to follow. The brief is its whole first prompt, so write it
self-contained (briefs.md). The agent starts fresh, never with your history. A spawn past
`--fanout` live subagents is refused; wait for a handoff, or `stop_agent` one you no longer need.

Choose the harness per role. This is guidance, not a rule:

- Leave `harness`, `model` and `effort` empty to run as you do. Set `model` and `effort` only
  with a reason, and only together with a `harness`: they apply to that harness alone.
- Workers do the volume of the work and wait on measurements: prefer the cheaper or faster
  harness available, and raise `effort` only for a bold plan.
- Judges should not be the model that wrote the change. Pick a different `harness` from the
  worker's so the judge reads the diff without its blind spots; with one harness available, a
  different `model`.
- Planners and combiners take your own harness unless a plan needs another's strength.
- The brief you were given may carry a steering note from the operator about harnesses, models
  or cost. It outranks this guidance.

`list_agents` shows each agent of the run with its role, status and plan. `send_message` with
`agent_id` and `text` steers a working agent, or starts the next turn of an idle one; use it to
correct a worker's course or to ask a finished agent a follow-up. `stop_agent` ends an agent:
stop one whose plan you closed or whose claim a judge failed, so it frees its slot. Do not
re-spawn an agent that `list_agents` still shows working.

## Run a long command as a tracked background task

Submit jobs with `vibesys-cli submit`; the CLI queues and runs them. Nothing notifies you when a
job ends. Plan or dispatch while it runs, and read it with `vibesys-cli jobs` or `show` when a
handoff wakes you. Wait in the foreground with `vibesys-cli wait` only when you have nothing
else to do. Workers and judges do the same inside their own turn: they submit, `wait` and
measure before they hand off.

## Schedule periodic work

There is no scheduler. Send the hourly report with `post_report` (`headline`, `changed`,
`in_flight`, `decisions`, `open_for_user`) when a handoff wakes you and an hour has passed since
the last, and after each promotion. The operator reads it in the run; it is not a question and
needs no reply.

## Wait for work without polling

A subagent finishes with `post_handoff`: its claim, commit, checks, measurement, what it
learned and what is left arrive as a message that wakes you. When no handoff is due, end your
turn; the run starts your next turn on the next handoff or operator message. Do not loop on
`list_agents`, `sleep` or `vibesys-cli jobs`. While you wait, a subagent cannot reach you except
by its handoff, so a worker that must end early has to hand off with what it has. If you are
a subagent, call `post_handoff` once, last, and end your turn.

## Packaging

VibeSys installs these skills for its agents; there is nothing to install. The operator starts
and stops the run; to stop yourself, bring every plan up to date and end your turn.
