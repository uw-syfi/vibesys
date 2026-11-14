# VibeSys orchestrator

`VIBESYS_RUN` is set, so VibeSys hosts the run: it starts every agent, gives
each the tools below through the `vibesys` tool server, and sets `VIBESYS_AGENT` and
`VIBESYS_RUN` for it. Use these tools for every subagent. Your own
harness's subagent tools would start agents the run cannot see, stop, or count.

## Dispatch a subagent with a fresh context

Call `spawn_agent` with `role`, `brief` and optionally `plan_number`, `harness`, `model` and
`effort`. The roles are `planner`, `worker`, `judge`, `combiner` and `profiler`; use the one that
matches the skill you want the agent to follow. The brief is its whole first prompt, so write it
self-contained (briefs.md). The agent starts fresh, never with your history. This is the only
way to start a planner, a worker or a judge: never load a role's skill yourself in place of
spawning it, and never implement a plan in a worktree of your own. A spawn past
`--fanout` live subagents is refused; wait for a handoff, or `stop_agent` one you no longer need.

Leave `harness`, `model` and `effort` empty. The run has an allowance of harnesses and models,
and an empty model takes the first model the allowance lists for the harness. Name a `harness`
or a `model` only when the allowance offers more than one choice and a role has a reason for
one of them; you never name a model the allowance does not list. A spawn outside the allowance is
refused with the list of what is allowed: choose from that list, or leave the fields empty.

- `model` and `effort` apply to the `harness` they are given with.
- With a choice, workers do the volume of the work and wait on measurements: prefer the cheaper
  or faster one, and raise `effort` only for a bold plan.
- With a choice, a judge should not be the model that wrote the change: a different `harness`
  from the worker's, or with one harness, a different `model`. With no choice, an empty spawn is
  right.
- The brief you were given may carry a steering note from the operator about harnesses, models
  or cost. It outranks this guidance.

`list_agents` shows each agent of the run with its role, status and plan. `send_message` with
`agent_id` and `text` steers a working agent, or starts the next turn of an idle one; use it to
correct a worker's course or to ask a finished agent a follow-up. `stop_agent` ends an agent:
stop one whose plan you closed or whose claim a judge failed, so it frees its slot. Do not
re-spawn an agent that `list_agents` still shows working.

## Run a long command as a tracked background task

Submit jobs with `vibesys-cli submit`; the CLI queues and runs them. The orchestrator checks the
run's job feed. When it observes a job in a final state while the run is live, it sends one
completion message and wakes you; repeated checks do not send it again. Plan or dispatch while the
job runs, then read it with `vibesys-cli jobs` or `show` after that wake. Do not add a polling loop
or provider timer to detect completion. Wait in the foreground with `vibesys-cli wait` only when
you have nothing else to do. Workers and judges do the same inside their own turn: they submit,
`wait` and measure before they hand off.

Every measure you or an agent submits in this run says what it tests: `--plan <n>` for the plan
it is a try of, `--no-plan` only for the baseline and a re-measure of the head. `vibesysd` refuses
a measure from a run that gives neither, naming both flags, so the run's chart can show every
point beside its idea.

## Schedule periodic work

You set no timer for the progress report: the run asks for it. When work has moved since your last
report (a subagent started, a handoff or a job's end reached you) and the run's report interval has
passed since that report, you are sent a message that says a progress report is due. The interval
is the run's own (one hour unless the operator chose another, never under five minutes), and the
message says it. Answer with `post_report` (`kind` `progress`, `headline`, `changed`, `in_flight`,
`decisions`), covering the whole run, and send one after each promotion as well.
Any report you send, asked or not, starts the interval again. A run where nothing moved is not
asked. The operator reads the report in the run; it is not a question and needs no reply.

The operator can also press Catch me up, and you are then sent a message that names a span of time
(from when they last looked to now). Answer it in the turn it starts with one `post_report` of kind
`progress` that covers that span and not the whole run, in the same fields. The request is tied to
the report you post in its turn, so a report you posted before it does not answer it. It needs no
other work from you.

Every report has a `kind`. Use `progress` for the periodic report and the note after a promotion:
the run goes on. Use `final` once, as your last report, when the SKILL's Finish rule holds, or
when an external factor you cannot fix stops all progress (no disk space, no quota, a host that is gone) and name that cause in its headline. An operator or a script that waits for the
result waits for the `final` report; a `progress` report never satisfies it, so do not mark a
note along the way `final`, and do not end the work without one. A progress report after every
job or handoff is noise: send it after a promotion or when the run asks for one.

Do not end a turn with a provider wake-up or timer of your own (such as `ScheduleWakeup`) as a way
to wait: the run starts none for you, and the run wakes you on every handoff, finished job and
operator message. A run whose lead rests with no agent working, no job running and no message
waiting is a stalled run, and a lead that ends a turn with open plans and nothing outstanding
makes one. It shows as stalled to the operator, and after ten minutes you are sent a reminder
that names the next step, at most three times in all. Act on a reminder: start that step, or
send a `final` report saying why there is none. After a `final` report you rest without
reminders until you are given something new.

## Wait for work without polling

A subagent finishes with `post_handoff`: its claim, what it learned, what is left and the
body of its role (a worker's commit, checks and measurement or why it has none; a judge's
verdict and reasons; a planner's plan file; a combiner's commit; a profiler's answer) arrive as
a message that wakes you. When no handoff is due, end your
turn; the run starts your next turn on the next handoff or operator message. Do not loop on
`list_agents`, `sleep` or `vibesys-cli jobs`. While you wait, a subagent cannot reach you except
by its handoff, so a worker that must end early has to hand off with what it has. If you are
a subagent, call `post_handoff` once, last, and end your turn. Fill in the field named for your
role (`worker`, `planner`, `judge`, `combiner` or `profiler`) and no other; a call with another
role's field, or with a required argument missing, is refused and says what to fix. A subagent that ends a turn without
a handoff is reminded once to send one, and its lead is told if it does not.

## Packaging

VibeSys installs the `vibesys` skills for its agents; there is nothing to install. No domain
plugin is installed: a domain skill reaches you only by the path the operator's brief names
([domain-plugins.md](../domain-plugins.md)). The operator starts and stops the run; to stop yourself, bring every plan up to date and end your turn.
