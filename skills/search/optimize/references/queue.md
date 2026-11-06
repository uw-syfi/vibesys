# Steering the queue

Slots are scarce, and the order jobs start in is yours to decide. The daemon orders the jobs that
may start by priority, then (within one priority) by its own policy; it never stops a running job,
and a measure still runs alone. Only you, the lead, steer: workers never change a priority, and
never hold, release or cancel a job they did not submit.

## Read the queue first

`vibesys-cli queue` lists the task's queued and running jobs grouped by plan (number, title,
status, priority), each with its kind, commit, where it runs or why it waits, whether it is held,
its submitter, its note, its age and an estimate from this task's history of jobs of its kind.
Read it before you fund more work, and before you reprioritize. A queued job with a wait reason is
waiting its turn, not stuck. A lab (`lab for <time>`) is listed, prioritized, held and drained as
any job; a running lab holds its slot, and its GPUs, until it is ended or its time runs out.

## Set a priority when you fund a plan

Every priority change needs `--reason`, which the CLI keeps (a plan comment, or a note on the
job) with your agent name and the time.

- `vibesys-cli plan edit <n> --priority high --reason "<why>"` for work that gates others (an
  integration line, a combine, a re-measure a promotion waits on) or that has the best expected
  value per GPU-minute.
- `--priority low` for exploratory work: microbenchmarks, feasibility probes, labs and profiles
  that inform no pending decision.
- Leave the rest `normal` (a new plan's priority). Set the plan's priority, not each job's: every
  job linked to the plan with `--plan` takes it.
- `vibesys-cli job priority <job> high|normal|low|inherit --reason "<why>"` overrides one queued
  job, such as the one check of a plan that now gates a promotion.

A low job is not starved: a job that waits counts one level higher per hour waited. So a low
priority delays exploratory work behind gating work; it does not park it. To park a job, hold it.

## Reorder without cancelling

Never cancel a queued job to reorder the queue: cancelling loses its place and makes an agent
resubmit it. Instead:

- Raise or lower a priority, as above.
- `vibesys-cli hold <job> --reason "<why>"` keeps one queued job from starting while it keeps its
  place; `vibesys-cli release <job>` lets it start in its turn. A held job holds back no other job.

Cancel only a job whose result nobody needs any more, and say why with `--reason`.

## Roll out a new CLI without cancelling

When the CLI must be replaced while jobs are queued (a fix you need, a new build the user gives
you):

1. `vibesys-cli daemon drain --reason "rolling out a new CLI"`: no queued job starts; running jobs
   go on.
2. Wait for the running jobs (`vibesys-cli queue` lists them) with `vibesys-cli wait <job>...` as
   a tracked background task, never a polling loop; then `vibesys-cli daemon status` says
   `running 0`. A running lab ends only when its owner ends it or its time runs out, so ask its
   worker to `lab end` it. A running job or lab survives a restart too, so you may restart sooner
   when you must. A lab queued during the drain stays queued, and its `lab start --wait` exits 1
   saying the daemon drains.
3. Install the new build, then `vibesys-cli daemon restart`. The new daemon reads the drain and
   starts nothing.
4. `vibesys-cli daemon status` must still say `draining`, and `vibesys-cli queue` must list the
   same queued jobs. Then `vibesys-cli daemon resume`.

Tell the workers nothing needs resubmitting: their jobs and labs kept their places, and a worker
whose lab was queued runs `vibesys-cli lab ps <lab> --wait` again.
