# vibesys-cli

`vibesys-cli` is a standalone Go binary. The VibeSys optimization skills call it for anything that
must be deterministic: measuring a commit, comparing results, promoting a winner, and the
lifecycle of the jobs that do that work. The skills hold judgment (what to try, when to stop); the
CLI holds determinism. It does not launch an agent.

The Python package installs a different command, `vibesys`, and `vibesys compare` means something
else there. The Go binary keeps the name `vibesys-cli`.

Work is landing in stages. The module has the foundation (the layout, the layer rules, the
contract, the domain types, the interfaces, and the two `Store` implementations with the contract
suite they share), the daemon, the per-job supervisor and the job lifecycle commands, and now real
jobs on the host or in docker: an agent can submit a command, check or measure job, watch it, read
its output, cancel it, and find it again after the client, the daemon or the machine went away, and a
measure reuses the samples and the check pass that earlier jobs saved. See "What this build runs"
below.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/uw-syfi/vibesys/main/cli/install.sh | sh
```

The script picks the binary for your operating system (Linux or macOS) and architecture (amd64 or
arm64), checks it against the SHA-256 the release publishes, and installs it as
`~/.local/bin/vibesys-cli`. It needs `curl` or `wget` and `sha256sum` or `shasum`, never asks for
root, and does not edit your shell profile; if `~/.local/bin` is not on your `PATH` it says so.
A download that does not match the published checksum installs nothing and leaves any earlier
install in place.

| Setting | Effect |
| --- | --- |
| `VIBESYS_CLI_VERSION=0.2.0` | Install that version instead of the newest. |
| `VIBESYS_CLI_INSTALL_DIR=/path` | Install there instead of `~/.local/bin`. |
| `VIBESYS_CLI_RELEASES_URL=https://host/path` | Read release assets from a mirror. The default is `https://github.com/uw-syfi/vibesys/releases/download`. |

Check what you installed:

```sh
vibesys-cli --version
```

The checksum guards against a damaged or truncated download. To check that a binary was built by
this repository's release workflow, verify its build attestation with the GitHub CLI:

```sh
gh attestation verify ~/.local/bin/vibesys-cli --repo uw-syfi/vibesys
```

## Upgrade

```sh
vibesys-cli upgrade
```

It reads the newest release, and if its version differs from the running binary's it downloads the
binary for this platform, checks the SHA-256 against the release's `checksums.txt`, and renames it
over the running file. The rename is atomic, so a process that is starting sees the old binary or
the new one, never half of one. A download that is cut short, corrupted, or has no published
checksum is refused with an error and the installed binary is untouched. If the directory holding
the binary is not writable (for example `/usr/local/bin`), the command says so; reinstall with
`install.sh` into a directory you own. A development build reports version `dev` and upgrades to
the newest release.

## Releases

A tag `cli-v<version>` (for example `cli-v0.2.0`) starts `.github/workflows/cli-release.yml`. It
checks the module, builds a static binary for each of linux/amd64, linux/arm64, darwin/amd64 and
darwin/arm64 with `cli/build_release.sh`, attests them, and publishes a release for the version.
A version with a suffix (`cli-v0.2.0-rc.1`) is a GitHub pre-release and is not offered to anyone
who has not asked for it by version.

The files have the same names in every release, with no version in them:

| File | Content |
| --- | --- |
| `vibesys-cli_<os>_<arch>` | The binary. |
| `checksums.txt` | `sha256sum` output for the four binaries. |
| `VERSION` | The release version. |

The newest stable release is also published under the tag `cli-latest`, so
`https://github.com/uw-syfi/vibesys/releases/download/cli-latest/<file>` is a fixed address.
GitHub's own `releases/latest/download/<file>` is not used: this repository also publishes
releases of the Python package, and the marker goes to the newest release created, whichever it
is. Publishing details are in [docs/contributing/publishing.md](../docs/contributing/publishing.md).

## Layout

| Path | Role |
| --- | --- |
| `cmd/vibesys-cli/` | Argument parsing and output. Nothing else. |
| `internal/core/` | Pure domain types and rules (job keys, state machine, cache reuse, verdicts, promotion checks): no I/O, no clock, no goroutines. |
| `internal/store/` | `Store`: the logical interface to results, events, logs, job state and plans. No paths, no formats. |
| `internal/store/ledger/` | The decisions every `Store` shares (what a submission joins or reuses, numbering, what a value may hold), as a pure `Ledger`, and the `Engine` that runs one over a `Backend`. It asks `core` whether a transition is legal (`Job.Apply`) and whether a submission joins a job (`CanJoin`). |
| `internal/store/memstore/` | In-memory `Store` for tests. The same `Engine` as `filestore`, with a backend in memory. |
| `internal/store/filestore/` | The only code that knows the on-disk layout. Owns migrations. Its package comment states the layout, the commit protocol and the filesystem operations it relies on. |
| `internal/store/filestore/fsys/` | The slice of a filesystem `filestore` uses, the operating system's implementation, and an in-memory one that models what a crash keeps and fails or dies at any write step. |
| `internal/store/storetest/` | The one contract suite every `Store` passes, and a differential driver that compares stores. |
| `internal/environment/` | `Environment`: where a job's commands run (`host`, `docker`), and `Registry`, the environments a build has by kind. |
| `internal/environment/host/` | The host `Environment`: runs a command on this machine in its own process group, and ends the whole group on every path. Passes the environment contract suite. |
| `internal/backend/` | `Backend`: facts about the compute hardware a task targets. |
| `internal/backend/cpu/` | The CPU `Backend`: the machine's facts and load, read through a `System` seam with a fake. |
| `internal/task/` | Reading a task from `.vibesys/tasks/<task>/` as a commit has it (unknown keys, `[evaluator]` and `entrypoint` tasks are refused), its version, and `Source`, how a task is fetched. |
| `internal/reading/` | Reading a benchmark's result: one field of a JSON file, or the records of an evaluator result stream. |
| `internal/flow/` | The check and measure flows as pure step functions (`State`, event in, `State` and requests out), and the `Plan` a job is run from. No clock, goroutines or files. |
| `internal/flowrun/` | The shell that carries a flow's requests out on an `Environment`, a `Backend` and a file system, and nothing else. A `Fake` environment passes the same scenarios as the real one. |
| `internal/hostfs/` | The file system a job reads results from, with the operating system's and an in-memory one. |
| `internal/gitwork/` | The git program as a source of tasks (`git archive`) and of clean checkouts of a commit (`git clone`, then a detached checkout). The working tree of the repository is never read or touched. |
| `internal/lifecycle/` | The job lifecycle as a pure machine: `Step(State, Event) (State, []Request)`. The daemon feeds it events and carries out the requests; recovery after a crash is the same events replayed. |
| `internal/changed/` | Pure helpers for the "what changed" facts a job records. |
| `internal/clock/` | `Clock` and `Timers`, the only ways the daemon and the client read or wait on time. A test supplies its own and fires timers itself. |
| `internal/owned/` | `Group`, the one way the daemon's packages start a goroutine: the group starts it and `Wait` joins it, so nothing outlives what started it. |
| `internal/gitrepo/` | `Git`: the repository containing a directory, and a revision resolved to a full commit id. |
| `internal/runtimedir/` | The per-user runtime directory (socket, locks, supervisor directories), its safety checks, and the advisory file locks. |
| `internal/environment/fakeenv/` | A `Fake` `Environment`, a real implementation that runs scripted commands in memory. Tests only. |
| `internal/environment/docker/` | The `docker` `Environment`: builds the task's Dockerfile into an image cached by content, runs a job's commands in one container over a clean checkout as the invoking user, and removes the container on every path. It decides over an `Engine`, the narrow interface to Docker. |
| `internal/environment/docker/dockercli/` | The `Engine` that drives the `docker` program. The only place that touches a real Docker. Its tests against a real daemon carry the `docker` build tag. |
| `internal/environment/docker/dockerfake/` | An `Engine` that keeps a Docker daemon in memory and can fail or crash any call. Tests only. |
| `internal/environment/docker/dockertest/` | The one contract suite every `Engine` passes. Tests only. |
| `internal/wire/` | Translation between `core` types and the generated contract types, the version handshake, and typed refusals. |
| `internal/supervisor/` | The supervisor as a domain type: it owns one job's command and its output, and reports the outcome. |
| `internal/supervisor/rpc/` | The supervisor's gRPC server and client, over the additive `supervisor.proto`. |
| `internal/supervisor/host/` | Starting supervisor processes, finding the ones still alive after a daemon restart, and serving a supervisor on its socket. |
| `internal/supervisor/suptest/` | The contract suite every supervisor implementation passes. Tests only. |
| `internal/app/` | The daemon's job flows (submit, cancel, watch, logs, recovery on start) over `Store`, `Supervisors` and `Environments`, with no transport. |
| `internal/app/filestores/` | `app.Stores` over `filestore`: one store per repository and task, and the per-user index that names them. The shipped binary's stores. |
| `internal/app/memstores/` | `app.Stores` over `memstore`, standing for the disk. Tests only: `production` does not import it, so the binary ships no in-memory store. |
| `internal/daemon/` | The daemon as a process: the gRPC server over `app`, the single-instance lock, the readiness signal, and a clean shutdown. |
| `internal/daemon/daemontest/` | A daemon started in a test's process on a test directory. Tests only. |
| `internal/client/` | What a command line calls: connect (starting the daemon under a lock when none runs), the version handshake, and `Wait`. It speaks the contract's messages and never imports `core`. |
| `internal/production/` | The wiring of the shipped binary: which environment, stores and supervisors the daemon runs with. |
| `internal/upgrade/` | `vibesys-cli upgrade`: the release asset names, a `Source` of releases (GitHub over HTTP, or the in-memory fake in `upgradetest/`), and the verify-then-rename install. |
| `internal/sim/` | The whole-daemon simulation: the real `app`, server and client over Fakes, driven by a seed. Tests only; it has no product code. |
| `internal/archcheck/` | The layer, layout, clock, goroutine and fuzzing rules below, as a check `go test` runs. |
| `gen/` | Generated Go for `proto/vibesys/cli/v1`. Never edited by hand. |

## Layer rules

Dependencies point one way: `cmd` and the shell packages, then interfaces, then `core`.

- `core` imports nothing from this module, only the standard library.
- `store`, `environment` and `backend` import `core` only. They declare an interface and a contract;
  implementations live in sub-packages (`filestore`, `memstore`) or later packages and import their
  interface's package.
- `ledger` imports `core` and `store`. `memstore` and `filestore` import `ledger`, and `filestore`
  its `fsys` seam; they never import each other. The contract suite `storetest` imports `core` and
  `store` only, and only test files import it (and `filestore`'s tests, `memstore` to compare with).
- `wire` holds every conversion between `core` and `gen`. Domain types, the generated contract
  types and the on-disk encoding are three separate models; none of them is another's
  representation. The adapters that serve the contract (`daemon`, `supervisor/rpc`,
  `supervisor/host`) import both, because they sit exactly on that boundary and call `wire` for each
  conversion. `cmd/vibesys-cli` and `client` speak the generated messages only and never import
  `core`.
- `gen` imports nothing from this module.
- `lifecycle` imports `core` only, so the machine stays pure. `app` imports no transport:
  `daemon` and `supervisor/rpc` are the shells around it, and neither imports the other's peer
  (`client` and `daemon` do not import each other, `app` does not import `daemon`).
- Packages that exist for tests (`fakeenv`, `suptest`, `daemontest`, `storetest`) are imported by
  test files only. `production` is imported by `cmd/vibesys-cli` only.
- Storage layout (state directory names, file names, the per-user index location) appears only in
  `internal/store/filestore`. Every other package reads and writes through `Store`.

- The real clock is reached only through `internal/clock`. No other package calls `time.Now`,
  `Since`, `Until`, `Sleep`, `After`, `AfterFunc`, `Tick`, `NewTimer` or `NewTicker`, in product code
  or in tests; `time.Date`, `time.Unix` and the duration types are fine. A daemon that read the real
  clock could not be replayed from a seed.
- A goroutine in `app`, `daemon` and `supervisor` (and their sub-packages) starts through
  `owned.Group`, never with a bare `go` statement, so every goroutine has an owner that joins it.
  Test files are not held to this: a test starts its own client goroutines and bounds them.
- Every exported `To*` decoder in `internal/wire` (what turns a peer's message into a domain value)
  is called by a fuzz target in `internal/wire/fuzz_test.go`, or is listed in `archcheck` with the
  reason a fuzz target already covers it.

`internal/archcheck` enforces all of these. A new package needs an entry in its rules table in the
same change, so an unlisted package fails the test instead of passing unchecked. Imports that only
test files may use are listed separately, so a fake never ships. A file that must break the clock
or goroutine rule is listed in `archcheck` with its reason, and an entry whose file no longer
breaks the rule fails the check until it is removed.

## Commands

Flags come after the command name. Every command takes `--json`, and these context flags, which
default to the environment variables shown:

| Flag | Environment | Meaning |
| --- | --- | --- |
| `--repo` | `VIBESYS_REPO` | The git repository. Defaults to the one containing the current directory. |
| `--task` | `VIBESYS_TASK` | The task the jobs belong to. Required. |
| `--run` | `VIBESYS_RUN` | The id of the optimization run. |
| `--harness` | `VIBESYS_HARNESS` | The agent harness, such as `claude-code`. |
| `--agent` | `VIBESYS_AGENT` | The agent making the call. |

| Command | What it does |
| --- | --- |
| `submit check <commit> [flags]` | Queue the task's check. A pass already saved for the commit answers it without running. Also `--wait`, `--note`, `--no-cache`, `--plan` and `--env`. |
| `submit measure <commit> [--samples n] [--recheck] [flags]` | Queue the task's benchmark for `n` samples (the task's default when omitted). Saved samples count: only the missing ones run. Flags as for `check`. |
| `submit command <commit> [flags] -- <argv...>` | Queue a job and print its id. Returns at once; `--wait` blocks as `wait` does. Also `--note`, `--no-cache`, `--plan`, `--env`, `--timeout`, `--service` and `--probe`. A revision is resolved to a full commit id. |
| `wait <job>... [--any] [--timeout d]` | Block until the jobs are final, or with `--any` until one is. |
| `cancel <job> [--reason text]` | Stop a job and print it once it is final. Cancelling a final job prints it unchanged. |
| `note <job> <text>` | Add a note to a job in any state. |
| `jobs [--commit rev] [--state s] [--note-contains text]` | List the task's jobs, oldest first. |
| `logs <job> [--follow]` | Print a job's output, and with `--follow` keep printing until it ends. |
| `show <commit>` | Every result saved for a commit, checks and measurements, oldest first. |
| `compare <commit> [--against rev]` | `better`, `worse`, `within_noise` or `inconclusive`, against the result branch head unless `--against` names a commit. Refuses to mix environments. |
| `promote <commit>` | Fast-forward the result branch to a commit that is `better` than its head, or print every reason it was refused. A refusal exits `1` and is printed on stdout. |
| `result` | The head and a final number measured after the head last moved, or why there is none (exit `1`). |
| `plan create --title t [--body text \| --body-file path]` | Store a plan, open, and print its number. `--body -` and `--body-file -` read standard input. |
| `plan list [--status s] [--search text]` | List the task's plans by number. With no `--status` every plan is listed. |
| `plan view <n>` | Print a plan, its comments and the jobs linked to it (`submit --plan <n>` links one). |
| `plan edit <n> [--title t] [--body text \| --body-file path] [--status s]` | Change what is given and keep the rest. |
| `plan comment <n> --body text \| --body-file path` | Add a comment, which carries a decision or a result. |
| `plan close <n> --status s [--comment text \| --comment-file path]` | Set a status other than `open` and add a comment, as one change. |
| `daemon status\|stop\|restart` | Report on, stop, or restart the daemon. `daemon install` is not available yet. |
| `daemon forget <repository>` | Give up a repository that is gone for good, such as a removed worktree: its commands are ended, then its jobs leave the daemon, what is left of their supervisors is let go of, and its stores leave the index. A command that does not end when told is reported left, and the next job waits while it can run, up to its time limit. Nothing is touched where the repository was. A repository that is there, one whose directory cannot be read (a disk that is not mounted or does not answer), or one the daemon does not know, is refused: only a repository whose directory is not there is gone. |

Plans belong to a task, are numbered 1, 2, ... by the daemon and never renumbered, and take one of
five statuses: `open`, `chosen`, `won`, `parked` and `rejected`. The CLI orders none of them, so an
agent may move a plan from any status to any other, and it stores the body and the comments as they
are given. A plan number may be written `3` or `#3`. `plan view` shows the jobs linked to a plan; it
does not show their verdicts, so run `compare` on a linked job's commit for one. A submission or `plan create` makes a task's store, so a planner can write plans before
the first job. The other plan commands refuse a task with no store with a "no store" error, and
`plan list` prints nothing for it.

Exit codes: `0` ok (for `wait`, every job it waited on succeeded), `1` failed (a job did not
succeed, or the call was refused), `2` invalid input or a job that does not exist, `3` a `wait`
timed out, `4` a `wait` gave up without learning how its jobs ended, which says nothing about them:
they may still succeed. A `wait` whose connection to the daemon breaks connects again, within its
`--timeout` if it has one and for about a minute since it last heard anything if not.

With `--json`, a command prints the contract's messages in protobuf's canonical JSON form with the
original field names, one compact message per line: the response for `submit`, `cancel`, `note`,
`jobs`, `logs`, `show`, `compare`, `promote`, `result`, `daemon status` and the `plan`
subcommands; one `WatchResponse` per job for `wait` (the contract has no message for it); and, on
stderr, a `Failure` for an error that a command reports: a refusal from
the daemon, a missing argument, a job that does not exist, a `wait` that gave up (exit `4`).

Some errors are plain text on stderr even with `--json`, because they come before the command has
read its flags or are not errors of a call: a flag that does not parse (exit `2`, printed by Go's
`flag` package), an unknown command or none (the usage text, exit `2`), and the note that a `wait`
timed out (exit `3`; the jobs still print as `WatchResponse`s on stdout). A caller reads the exit
code first and parses stderr as a `Failure` only for `1`, `2` and `4`, and only when it parses.

Text output rounds numbers to four significant digits. `--json` and the verdicts behind it carry
the exact values.

A signal ends the command and nothing more. A job the command submitted or waits on keeps running.

## The daemon and the supervisors

There is one daemon per user per machine. It listens on a Unix socket in a private (`0700`) runtime
directory on local disk: `$XDG_RUNTIME_DIR/vibesys-cli` when that is set to an absolute path, and
otherwise `/tmp/vibesys-cli-<uid>`. A directory on a network file system is refused, because
sockets and locks are not reliable there. The directory holds:

| File | Role |
| --- | --- |
| `daemon.sock` | The gRPC socket. |
| `daemon.lock` | Held by the one running daemon. The operating system drops it when the daemon dies, so a crash never leaves a stale lock. |
| `start.lock` | Held by a client while it starts a daemon, so clients that find none at once start one. |
| `daemon.log` | The diagnostics of a daemon a client started. |
| `sup/` | One directory per running job, with its supervisor's socket and output spool. |

A command that needs the daemon connects and, if none runs, starts one and waits for its readiness
signal. A daemon that fails to start closes the signal without it, so the client reports the failure
at once and does not wait out a timeout. The client and the daemon exchange their versions when they
connect, and a mismatch is a refusal that says which side is older and what to run: `daemon restart` for an old daemon, an upgrade for an old client.

Each running job has its own supervisor process. It owns the job's process group and keeps the
job's output, so it outlives the daemon. The queue is first in, first out; the daemon runs one job
at a time and takes each decision from `lifecycle`.

Recovery is the daemon's start, with no separate path:

- The daemon lists the stores (the per-user index names them) and the supervisors left in the
  runtime directory, and joins them by job. `Supervisors.Runs` lists the run directories by name and
  probes each one; a probe also reads the run's `launch.pb` on a best-effort basis, only to learn
  which repository and task a run belongs to when no store lists its job. A run whose launch cannot
  be read has none, and recovery treats it as a run with no known store. `Runs` does not decide
  anything from the file.
- Queued jobs are in the store, so they run after a restart or a reboot.
- A running job whose supervisor is alive is re-adopted: the daemon follows it again, and if the
  job finished while the daemon was down, it records the outcome.
- A running job whose supervisor is gone (the machine rebooted, or the supervisor was killed)
  becomes `lost`, with one explanatory line in its output. A lost job is never retried: the
  command may have had effects.
- A supervisor that was started for a job the store never marked running is discarded.
- A store that cannot be read, such as a repository on a disk that is not mounted, stays known and
  is read again every slow period. None of its jobs starts, and a run of it is kept, not released.
  A run that is parked or finished runs nothing and holds up no other repository. A command that
  may still run holds the machine for as long as its job may run and no longer. A `wait` on a job
  held up this way ends with why (exit `4`) rather than waiting in silence.
- A repository that is gone for good is given up with `daemon forget`.

A store that goes away while the daemon runs stops no job: the supervisor's spool keeps the output,
and the daemon records it when the store is back. A job whose store is away at its turn waits for
the store.

A job's launch is ordered so any crash leaves one of those cases: the supervisor is started parked,
the store records the job running, and only then does the supervisor start the command.

## What this build runs

The shipped binary keeps its jobs on disk (`app/filestores`; the in-memory stores are test support
and are not linked into it) and runs them on the host, or in docker for a task that has a Dockerfile
(see "Running in docker" below). A job is one of:

- `submit command`: a command you give, run in a clean checkout of the commit. It is never cached.
- `submit check`: the task's `[accuracy]` command, run in a clean checkout. A pass is saved, and a
  second check of the same commit, task version and environment is answered from it.
- `submit measure`: the task's `[benchmark]` command, run once per sample after the check passes. The
  samples are saved with the machine's facts and load. When the measure ran the check itself, that
  pass counts as a check result (`core.Result.PassKey`), so a later check is answered by the
  measure job. A measure for more samples than the cache holds takes only the
  missing ones, and links the saved check pass instead of checking again.

`--no-cache` runs the whole job again, with a fresh check. `--recheck` (measure) checks again but
still takes only the missing samples. A cancelled job, a failed check, and a benchmark that wrote no
usable result save nothing, so a result is never saved without its samples and its check.

A task is read from `.vibesys/tasks/<task>/` of the job's commit, not from the working tree, and its
version (a digest of its files) is part of the cache key. A benchmark reports its score in one of two
ways: it writes a JSON file at the path given after the flag in `[benchmark.result]`, or, with
`result_protocol = 2` in `[benchmark]`, a stream of evaluator records at the path given after
`--vs-output`. Tasks that declare an `[evaluator]` or an `entrypoint` are refused: their commands
belong to an evaluator package. `${PROJECT_ROOT}` in a command is the checkout's root.

The lifecycle commands above work end to end against a `fakeenv` in tests, and the check and measure
flows run on the same fake in `app` and `daemontest`, so a test of the whole daemon needs no
process. `show`, `compare`, `promote` and `result` read what the stores hold, so they work on results
saved by any means.

### Running in docker

A task with a `Dockerfile` next to its manifest runs its jobs in the image that file builds, unless
the submission says `--env host`. Nothing falls back: a machine with no reachable Docker daemon
fails the job with a message that says so, and a task that needs what the image cannot give is
refused with the reason.

- The image is built from the Dockerfile as the job's commit has it, with no build context, and is
  cached by the Dockerfile's content. The first `submit` of a task builds the image before the job
  is queued, so that submission takes as long as the build. The image digest is recorded with each
  result and is part of what makes two results comparable, so results from different images are
  never mixed.
- A Dockerfile that copies files from the repository (`COPY` or `ADD` of a local path, or `RUN
  --mount`) is refused: the image must be the same for every commit that has the same Dockerfile.
- Commands run in a container as the invoking user, in a clean checkout of the exact commit. The
  checkout and the job's scratch directory are mounted at their own paths, so a path a command is
  given means the same inside. A task with `docker_in_docker = true` is refused: the container has
  no container runtime. A task for an accelerator backend is refused: containers get no GPU yet.
- Every container is named and labelled for the daemon that started it. A cancelled or timed out
  job removes its container, and the next job removes any container an earlier job left, for
  instance when the supervisor was killed. Containers that other tools started are never touched.
- The docker program must be on the `PATH` of the daemon, and the user must be allowed to use it.

## The result branch

Each task has a result branch, `vibesys/result/<task>`, in the repository the command runs in. Create
it at the commit the run starts from (`git branch vibesys/result/<task> <commit>`); a command that
needs it and does not find it says so. It must not be checked out. Only `promote` moves it, and only
forward: the new head must contain the old one, which a commit that does not is told as a
`conflict` failure.

`promote` decides from the saved measurements alone (internal/core), in this order: it is refused
with every reason that applies (`unmeasured`, `check_missing`, `worse`, `within_noise`,
`inconclusive`, `stale`, `environment_mismatch`), or it approves, which is a `core.Approved` that
only that decision makes and that a store requires to record a promotion. The daemon records the
promotion in the store and then moves the branch. The sequence is one pure function, `core.NextPromotionStep`: from the branch
head and the record it names the next effect (record, move, done, or diverged), and the daemon does
that effect and asks again. A crash or a failure at any point leaves a state the same question
answers, so a crash between the two leaves a recorded promotion the branch has not caught up with,
and the daemon completes the move the next time it looks at the branch (any of `compare`, `promote`
or `result`, or on start): the branch is never ahead of the record. Tests cut every effect off,
before and after it took place, in core over a model and in the daemon over the fake repository.
A branch someone moved by hand is diverged: nothing writes over it, it is the head the next
commands use, and `result` reports no number for a head the last promotion did not set.

## Contract

The contract is `proto/vibesys/cli/v1/`: the daemon's gRPC service, the job, result, verdict,
environment, event and plan messages, typed refusals, and the version handshake. The same messages
are the `--json` output of every command, in protobuf's canonical JSON form with the original field
names. Generated Go lives in `gen/`.

Regenerate it from the repository root:

```sh
buf generate --template buf.gen.cli.yaml
```

`./scripts/generate_proto.sh` runs it with the other templates, and `./scripts/check_proto.sh`
fails on a stale `gen/`.

## Checks

```sh
./check.sh
```

runs `gofmt` and `go vet`, vets for each release target (Linux and macOS, amd64 and arm64), then
runs the tests twice. The first run is every package under the race detector, which covers the
packages a later change adds without listing them; the tests that drive a real Docker daemon run
beside it, where a daemon is reachable. The second is every test again without the detector, as the
binary ships, with a wider range of simulation seeds. The tests include the architecture rules above,
building a real release with `build_release.sh` and installing it with `install.sh`, so they need
`go`, `curl`, `awk` and `sha256sum` or `shasum`. CI runs the same script as the `cli` job
(`cli_checks`, with a 600 s timeout and a 7 minute job limit).

A test that walks a large space of cases on one goroutine, such as the exhaustive model check of the
lifecycle machine or a crash of a store at every step of its script, is a sweep. The detector has
nothing to find in it and makes it ten times slower, so it lives in a file named `*_sweep_test.go`
that starts with `//go:build !race`, and runs in the second run only. `internal/archcheck` refuses
a sweep that starts a goroutine or imports `sync`, and a file that is built without the detector
and is not named as a sweep.

How the daemon is checked:

- The job lifecycle is a pure machine (`lifecycle.Step`) and the stores share one `Ledger`, so most
  rules are tested by property over many generated inputs, with no I/O.
- `internal/sim` runs the whole daemon over Fakes inside `testing/synctest`, driven by a seed: every
  store or supervisor call can fail or kill the daemon, every timer fires when the driver says, and
  invariants are checked after each step. A failing seed replays exactly: set `VIBESYS_SIM_SEED=<n>`
  to run just that seed. `VIBESYS_SIM_SEEDS=<count>` widens the default range. Its faults include
  repositories that go away while the daemon runs, every repository at once, a store that never
  answers, and a repository gone for good with a job running, and it checks that every job ends or
  its waiters are told why, and that no repository going away fails a job.
- `internal/lifecycle/model_test.go` checks the machine exhaustively for two jobs: every order of
  events up to a depth, for safety (one command at a time, nothing started beside a job its store
  shows running), the cancel a user asked for, and that a healthy world ends every job.
- The decoders in `internal/wire` have fuzz targets (`FuzzDecodeJob` and the others in
  `fuzz_test.go`). `go test` runs their seed inputs; to search for new failures run one for a while,
  for example `go test -run '^$' -fuzz '^FuzzDecodeJob$' -fuzztime 1m ./internal/wire`, and commit the
  file it leaves under `testdata/fuzz` when it finds one. The store has fuzz targets too
  (`internal/store/filestore`).
- A few tests drive real processes (the release build, `install.sh`, a real supervisor process), to
  show the Fakes match reality.

The tests open real stores under `TMPDIR`, and a store refuses a file system without working locks,
such as NFS mounted with `nolock`. Where `/tmp` is such a mount, set `TMPDIR` to a directory on a
local disk.

The module pins Go 1.25.9 with a `toolchain` line: under `testing/synctest`, Go 1.25.0 corrupts its
own runtime state at random, which the simulator hit as fatal errors and stalls.

The tests do not depend on time, threads or the network: the clock is injected, the crash tests fail
a step by its number on an in-memory filesystem so the same step fails on every run, and a wait in
a test is bounded only to guard against a hang. A real directory is used only to run the contract
suite once on the operating system's filesystem.
