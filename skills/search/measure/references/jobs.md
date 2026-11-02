# Jobs in detail

## What the CLI does for you

The CLI reads the task from the commit, builds and runs it in a clean checkout, runs one job at a
time for the machine, saves a result only with its samples and a passing check, and answers a
repeat of a job it already has from the saved result. A second submission of the same job joins
the one in the queue. `submit command` jobs are never reused or joined.

You therefore do not take a measurement slot, serialize your own runs, clean a worktree, start a
service by hand or record a sample. If a step seems missing, ask the CLI (`vibesys-cli --help`)
before writing a script.

## States

`queued`, `running`, `succeeded`, `failed`, `cancelled`, `lost`. `vibesys-cli jobs` filters by
`--commit`, `--state` and `--note-contains`. A `lost` job was running when the machine or its
supervisor went away; the CLI never retries it, because a command may have had effects.

## After a wait that did not end in 0

- 1: read `vibesys-cli logs <job>`. A failed job is evidence about its commit. Do not resubmit the
  same commit unchanged unless the log shows a cause outside the commit (a full disk, a killed
  process), and say so in a note.
- 3: nothing happened to the job. Wait again, or `vibesys-cli cancel <job> --reason "<why>"` if you
  no longer need it. Cancelling a shared job cancels it for everyone who waits on it, and a
  cancelled measure saves nothing.
- 4: the answer says nothing about the job: it was held, the outcome is in doubt, or the daemon
  could not be reached. The job may have finished, still run, or be rerun by the daemon itself
  where its retry rule allows, so a blind resubmit can duplicate work. Inspect with
  `vibesys-cli jobs --commit <sha> --json` or `vibesys-cli show <sha> --json`, and
  `vibesys-cli daemon status` if the daemon was unreachable. Resubmit only a `lost` job whose
  result you still need, once, with a note.

## Commands that drive a service

```
vibesys-cli submit command <sha> --service '<server argv>' --probe '<readiness argv>' \
  --timeout 10m -- <client argv>
```

The CLI starts the service, waits for the probe, runs the client, and stops the whole process
group on every exit. Keep the service in the foreground; disable daemonizing. Output you need
afterwards: print it and read it with `vibesys-cli logs <job>`; a file only helps where the
environment can write it.

## Slow or remote work

When one measurement is long (a cluster queue inside the task's own benchmark), submit it as soon
as the commit exists and wait with a long `--timeout`, rather than polling. Judge and compare on the
multi-sample result; a single development sample is a smoke test.

## Wait for several jobs

`vibesys-cli wait <job> <job>` returns when all are final; `--any` returns when one is. Use `--any`
to feed the next worker while the rest run.
