# Jobs in detail

## What the CLI does for you

The CLI reads the task from the commit, builds and runs it in a clean checkout, starts jobs as the
placement's capacity allows, saves a result only with its samples and a passing check, and answers a
repeat of a job it already has from the saved result. A second submission of the same job joins
the one in the queue; the submit response's `disposition` says `CREATED`, `JOINED` or `REUSED`.
`submit command` jobs are never reused or joined. A command job's time limit is its `--timeout`,
else its task's `command_timeout_seconds`, else 30 minutes; give a long profile or microbenchmark a
`--timeout` that covers it.

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
agents' work. A job that waits for GPUs says how many it wants and how many are free ("waiting for 4
GPUs of gpu:local, 2 free, held by ..."); `vibesys-cli placement capacity <placement>` shows the free
GPUs and which jobs hold the others, and a job asking for fewer GPUs starts sooner. Report a
refusal as the CLI printed it.

## Reading the queue

Before you submit more work or cancel anything, look at `vibesys-cli jobs --state queued,running`
and `vibesys-cli placement capacity <placement>`. Match each reason to an action:

| What `jobs` says | What it means | Do |
| --- | --- | --- |
| `waiting for N GPUs of <pool>, M free, held by ...` | The pool is busy | Wait, or resubmit at fewer GPUs if the command can run on fewer. The holders are other agents' work: never cancel them. |
| `behind <job>, which waits for ...` | An earlier job has the turn | Wait. A waiting job keeps its turn. |
| A measure needs the placement alone | It starts when the running jobs finish, then nothing else starts beside it | Wait. Do not queue more checks or commands behind it than you need. |
| A wait ends with a reason (exit 4) | The job can never run | Fix the request (a count larger than any pool, a placement that lacks a resource). Report the refusal as printed. |
| `superseded: does not contain result head <sha>` | A promotion moved the head after this measure was submitted, and its commit does not contain the new head, so `promote` refuses it whatever it measures | The lead decides: cancel it (`vibesys-cli cancel <job> --reason "superseded by <sha>"`), or dispatch a combine of the candidate onto the head and measure that. Keep it only when its number is still wanted as evidence. |

The `superseded` line appears under a queued or running measure only (with `--json`, in
`superseded`, each with its `result_head`); a measure of the head, of a commit built on it, or of a
commit the head already holds is never marked, and the CLI never cancels a job on its own.

If most of the pool is free while jobs queue, look for an oversized request (every job asking for
all GPUs). A long queue is not a reason to stop working: keep implementing and committing the next
ideas, so the best candidate is ready when a slot opens. A queued job runs the commit it was
submitted with and the CLI cannot change it, so it never picks up later work. On a placement where
jobs wait for hours, hold a standing allocation (`vibesys:allocations`) and submit the newest commit
into it when it is ready: the slot is held and the commit is chosen at submit time. Without an
allocation, when a newer commit of yours makes your own queued job's result useless, cancel that job
and submit the newer commit; it takes a new turn at the back. If the older result is still wanted,
leave it and queue both.

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
group on every exit. Keep the service in the foreground; disable daemonizing. Text you need
afterwards: print it and read it with `vibesys-cli logs <job>`. Files you need afterwards: see
the next section.

## Output files of a command job

A command job that produces structured results (per-shape kernel timings, a profile, a probe's
per-request results) declares them with `--outputs` and writes them under `$VIBESYS_ARTIFACTS_DIR`,
which every command job has, on the host and in a container alike. The argv is not run by a shell,
so name the variable through one or read it in your script:

```
vibesys-cli submit command <sha> --image harness --outputs --wait -- \
  sh -c 'python3 bench.py --out "$VIBESYS_ARTIFACTS_DIR/timings.json"'
vibesys-cli outputs <job>                 # the job, where it ran, each file's size and SHA-256
vibesys-cli outputs <job> --to <dir>      # copy them into <dir>, which you can write
```

The CLI records the files with the job when its command ends, success or failure, beside the
commit, environment and GPUs it ran with, so they are evidence of that commit. A job keeps at most
1000 files and 256 MiB in all, and only regular files: past a bound, or with a symbolic link in the
directory, it keeps none, and `outputs` says why, naming the bound and the size the job left. Write
only the results you need there (not a whole trace directory), and keep large captures out of it.
`outputs` refuses a job submitted without `--outputs` (exit 2) and one that has not ended (exit 1).
The CLI does not read the files; what they mean is your script's or the domain skill's format.

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
