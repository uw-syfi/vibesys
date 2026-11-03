# Task format

What `vibesys-cli` reads from `.vibesys/tasks/<task>/` at the job's commit. It reads the commit,
never the working tree, and refuses an unknown key with an error that names it. When this page and
the CLI disagree, the CLI is right: `vibesys-cli submit check <sha> --wait` on a commit with the
task is the test.

## Files

| File | Holds |
| --- | --- |
| `vibesys.input.toml` | The manifest: the commands and how the benchmark reports. Required. |
| `objectives.toml` | The scored metric's direction, and optionally the noise floor. |
| `OBJECTIVE.md` | The goal, scope, correctness bar and rules, for the agents. |
| `Dockerfile` | Optional. When present every job runs in the image it builds, unless `--env host`. |
| anything else | The checker, the benchmark, the reference, the inputs. |

Every file in the directory is part of the task's version. Results under two versions are never
compared, so a stray generated file in the directory splits the results; keep outputs elsewhere.
A file outside the directory is not part of the version, even when a command runs it: an edit to
a shared checker elsewhere would leave old results looking comparable. Copy every file the
commands run into the task directory.

## `vibesys.input.toml`

```toml
version = 1
backends = ["cpu"]            # one backend; containers get no GPU yet

[agent]
domain = "generic"            # which domain skills fit; held as data

[build]                       # optional; runs first, in the clean checkout
command = ["make", "-j8"]
timeout_seconds = 600

[accuracy]                    # the check; exit 0 is a pass
command = ["python3", ".vibesys/tasks/<task>/checker.py"]
timeout_seconds = 300

[benchmark]                   # one run per sample, after the check passed
command = ["python3", ".vibesys/tasks/<task>/benchmark.py"]
timeout_seconds = 600

[benchmark.result]
json_argument = "--output-json"   # the CLI appends this flag and a path
metric = "requests_per_sec"       # the top-level field of that JSON file
```

- Commands are argv lists, run from the checkout's root. The only placeholder is
  `${PROJECT_ROOT}`, the checkout's root.
- The benchmark is called with the `json_argument` flag and a path appended; it must write one
  JSON object there whose `metric` field is a number, and exit 0.
- `[check]` may stand for `[accuracy]` and `[measure]` for `[benchmark]`, never both.
- Refused by the CLI: `[evaluator]`, `entrypoint` or `args` in a command section,
  `requires_accuracy = false`, more than one backend, `[environment.modal]`, and in docker
  `docker_in_docker = true` or an accelerator backend.
- Accepted and not read by the CLI: `[agent]`, `[catalogue]`, `[profile]`, `[profile_guided]`,
  `[resources]`, `[workspace]`, `[plan]`, `[[comparison]]`.
- A benchmark may instead speak the evaluator record stream with `result_protocol = 2` in
  `[benchmark]`, and no `[benchmark.result]`. Use it only when the project already does.

## `objectives.toml`

```toml
[[objective]]
name = "requests_per_sec"   # equals [benchmark.result] metric
direction = "max"           # or "min"

[pareto]
relative_noise = 0.02       # optional: the relative spread of one run
```

With `relative_noise`, `compare` needs a gain above two standard errors of the ratio, with the
noise never taken below the floor. Without it, `compare` uses Welch's t test and answers
`inconclusive` until each side has three samples that are not all equal. Declare the floor from
the spread you measured (validation.md), not from hope: too low promotes noise, too high hides
real gains.

## Services and remote hardware

The check and the benchmark are plain commands; the CLI starts no service for them. A task whose
code is a server ships a trusted wrapper in the task directory that builds the candidate, starts
it through a contract the candidate implements (a start script, a port, a readiness endpoint),
waits for it, runs the checker or the load generator against it, and stops its whole process
group on every exit. Say the contract in `OBJECTIVE.md`.

Jobs run on the machine of the CLI's daemon, and docker jobs get no accelerator. When the
hardware is another machine, the wrapper copies the clean checkout there (by commit, so a check
and the samples of one commit can share a build), runs under that machine's scheduler if it has
one, copies the result file back, and cancels the remote work when it receives SIGTERM. Read the
host from an environment variable, never from the repository. Jobs inherit the daemon's
environment, not the submitting shell's: set the variable, then `vibesys-cli daemon restart`. The
clean checkouts go under the daemon's `TMPDIR`; point it at a local disk when `/tmp` is a network
mount.

## `Dockerfile`

Built with no build context, so no `COPY` or `ADD` of a local path and no `RUN --mount`. Pin the
base image by digest. Jobs run as the invoking user over a clean checkout mounted at its own path.
The first submit of a task builds the image and returns after the build.
