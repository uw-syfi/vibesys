# Jobs in detail

## What the CLI does for you

The CLI reads the task from the commit, builds and runs it in a clean checkout, starts jobs as the
placement's capacity allows, saves a result only with its samples and a passing check, and answers a
repeat of a job it already has from the saved result. A second submission of the same job joins
the one in the queue; the submit response's `disposition` says `CREATED`, `JOINED` or `REUSED`.
`submit command` jobs are never reused or joined.

You therefore do not take a measurement slot, serialize your own runs, clean a worktree, start a
service by hand or record a sample. If a step seems missing, ask the CLI (`vibesys-cli --help`)
before writing a script.

## Waiting jobs

The daemon may run several jobs at once (the operator's `max_jobs`, ordered `fifo` or
`fair-share`), but a measure job holds the whole placement, so a benchmark sample never runs beside
another job. A queued job is therefore not stuck: `vibesys-cli jobs` prints under it why it waits,
for instance that it needs the placement alone and which jobs hold it, or which earlier job it is
behind. A waiting job keeps its turn, and a wait on a job that can never run ends with why (exit
4). Read the reason before you resubmit or cancel; the jobs that hold the placement are other
agents' work. Report a refusal as the CLI printed it.

## States

`queued`, `running`, `succeeded`, `failed`, `cancelled`, `lost`. `vibesys-cli jobs` filters by
`--commit`, `--state` and `--note-contains`. A `lost` job was running when the machine or its
supervisor went away; the CLI never retries it, because a command may have had effects.

## After a wait that did not end in 0

- 1: read `vibesys-cli logs <job>`. A failed job is evidence about its commit. Do not resubmit the
  same commit unchanged unless the log shows a cause outside the commit (a full disk, a killed
  process), and say so in a note. A `lost` job is the exception: it was running when the machine
  or its supervisor went away, so resubmit it once, with a note, if you still need its result.
- 3: nothing happened to the job. Wait again, or `vibesys-cli cancel <job> --reason "<why>"` if you
  no longer need it. Cancelling a shared job cancels it for everyone who waits on it, and a
  cancelled measure saves nothing.
- 4: no daemon answered about the jobs: it could not be reached, speaks another version, failed, or
  could not learn how the jobs ended, and that says nothing about the jobs. They may have
  finished, still run or never started, so a blind resubmit can duplicate work. Inspect with
  `vibesys-cli jobs --commit <sha> --json` or `vibesys-cli show <sha> --json`, and
  `vibesys-cli daemon status` (exit 4 means no daemon runs; the next command starts one, and
  `daemon restart` replaces one of another version). Resubmit only what those commands show
  missing.

A submit that exits non-zero created no job. A task the CLI cannot run is refused at the first
submit, with a message that says why.
A `check` or `measure` of a commit whose task files (everything under `.vibesys/tasks/<task>/`
except generated files such as `__pycache__` and `*.pyc`) differ from the result branch head's is
refused with a `conflict` failure that names the paths. Restore those files from the base; never
adapt the task to the candidate.

## Commands that drive a service

```
vibesys-cli submit command <sha> --image harness --service '<server argv>' --probe '<readiness argv>' \
  --timeout 10m -- <client argv>
```

This is for a task without `[candidate]`, or a command in the candidate's image (`--image
candidate`). On a task with `[candidate]`, `--image harness` already runs the command beside the
candidate's server, as the benchmark does, and refuses `--service`. The CLI starts the service, waits for the probe, runs the client, and stops the whole process
group on every exit. Keep the service in the foreground; disable daemonizing. Output you need
afterwards: print it and read it with `vibesys-cli logs <job>`; a file only helps where the
environment can write it.

## First submit of a task with a Dockerfile

The CLI builds the task's image before it queues the first job, so that one submit returns after
the build. Later submits reuse the image. If Docker is unavailable the job fails; nothing falls
back to the host.

## Slow or remote work

When one measurement is long (a cluster queue inside the task's own benchmark), submit it as soon
as the commit exists and wait with a long `--timeout`, rather than polling. Judge and compare on the
multi-sample result; a single development sample is a smoke test.

## Wait for several jobs

`vibesys-cli wait <job> <job>` returns when all are final; `--any` returns when one is. Use `--any`
to feed the next worker while the rest run.
