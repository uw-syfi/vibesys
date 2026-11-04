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
| `internal/core/` | Pure domain types and rules (job keys, state machine, cache reuse, verdicts, promotion checks, frontier membership): no I/O, no clock, no goroutines. |
| `internal/store/` | `Store`: the logical interface to results, events, logs, job state, plans, promotions and the frontier. No paths, no formats. |
| `internal/store/ledger/` | The decisions every `Store` shares (what a submission joins or reuses, numbering, what a value may hold), as a pure `Ledger`, and the `Engine` that runs one over a `Backend`. It asks `core` whether a transition is legal (`Job.Apply`) and whether a submission joins a job (`CanJoin`). |
| `internal/store/memstore/` | In-memory `Store` for tests. The same `Engine` as `filestore`, with a backend in memory. |
| `internal/store/filestore/` | The only code that knows the on-disk layout. Every file is protojson of a message in `store.proto`, so no hand-kept record format exists. A store of format version 2 or 3 is migrated to version 4 when it is opened (version 3 only added the frontier and fields that a version 2 program would refuse, and version 4 a job's placement); a store of any other version is refused. Its package comment states the layout, the commit protocol and the filesystem operations it relies on. |
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
| `internal/schedule/` | Which waiting jobs start: `Step(State, Policy, ...Filter) []Decision`, pure. Jobs hold claims on resources (`placement:<name>` and what its resources claim); the core keeps every resource within its capacity and an exclusive claim alone, so a policy only orders. A `Demand` says what a job claims: `PlacementDemand` from the operator config's placements. |
| `internal/schedule/policy/fifo/`, `fairshare/` | The scheduling policies: `fifo` (oldest first) and `fair-share` (the run with the least recent use of what the job claims goes next). Only `production` names them. |
| `internal/schedule/scheduletest/` | The one property-based contract suite every policy passes. Tests only. |
| `internal/lease/` | `Leases`: where the daemons that share a resource take turns at holding it alone, and `Book`, where each resource is leased, which only `production` fills. |
| `internal/lease/locallease/` | The leases of this machine: `flock` on files of a directory every user shares on local disk (see "The daemon and the supervisors"). The kernel gives a lease back when its daemon dies. |
| `internal/lease/hostlease/` | The leases of a machine reached over ssh: the same lock files there, held by a shell for as long as its connection lives. |
| `internal/lease/kubelease/` | The leases of a Kubernetes cluster: `coordination.k8s.io` `Lease` objects, renewed every third of their duration and taken over once expired, through `kubectl`. `kubefake` is an API server in memory. |
| `internal/lease/fakelease/`, `leasetest/` | A `Leases` in memory, and the one contract suite every `Leases` passes. Tests only. |
| `internal/neighbors/` | The daemons of the machine, of every user, as `/proc` lists them, for `daemon status`. |
| `internal/changed/` | Pure helpers for the "what changed" facts a job records. |
| `internal/clock/` | `Clock` and `Timers`, the only ways the daemon and the client read or wait on time. A test supplies its own and fires timers itself. |
| `internal/owned/` | `Group`, the one way the daemon's packages start a goroutine: the group starts it and `Wait` joins it, so nothing outlives what started it. |
| `internal/gitrepo/` | `Git`: a revision resolved to a full commit id, in the caller's checkout; `CallerCommit` decides which worktree that is. `Resolver`: the repository a directory belongs to, through the common git directory, so every linked worktree of a repository resolves to one path. |
| `internal/runtimedir/` | The per-user runtime directory (socket, locks, supervisor directories), its safety checks, the advisory file locks, and where the machine's directory of leases is (`ResolveLeases`). |
| `internal/environment/fakeenv/` | A `Fake` `Environment`, a real implementation that runs scripted commands in memory. Tests only. |
| `internal/environment/docker/` | The `docker` `Environment`: builds the task's Dockerfile into an image cached by content, runs a job's commands in one container over a clean checkout as the invoking user, and removes the container on every path. It decides over an `Engine`, the narrow interface to Docker. |
| `internal/environment/docker/dockercli/` | The `Engine` that drives the `docker` program. The only place that touches a real Docker. Its tests against a real daemon carry the `docker` build tag. |
| `internal/environment/docker/dockerfake/` | An `Engine` that keeps a Docker daemon in memory and can fail or crash any call. Tests only. |
| `internal/environment/docker/dockertest/` | The one contract suite every `Engine` passes. Tests only. |
| `internal/environment/docker/machine/` | Where a docker engine's containers find a run's directories: `Local` (this machine's own directories, and its caches) and `Remote` (copies made with rsync on a machine reached over ssh, copied back when the command ends). |
| `internal/environment/slurm/` | The Slurm placement: an `Allocator` that runs a container's first process and every image build under `srun`, and an `Engine` that removes a container and then cancels and confirms the end of its allocation, and sweeps the allocations a dead run left. Passes the environment contract suite over the fake engine. |
| `internal/remote/` | Running programs on a machine and copying directories to and from it: this one, or another over ssh and rsync. An exit code that is ssh's own is never taken for the program's. |
| `internal/backend/nvidia/` | The GPU facts of a machine (model, count, driver, the driver's CUDA version) for a fingerprint, read with `nvidia-smi`. |
| `internal/operator/` | The operator config (`cli.toml`), decoded into `OperatorConfig` in `operator.proto` with unknown keys refused by name: its named placements, each placement's kind and sections, and a file of the earlier shape read as the placement `default`. What a kind's section means is that kind's package's business. |
| `internal/resource/` | `Resource`: what a placement gives every container (`Kind`, `Claims` for scheduling, `Fingerprint`, and `Attach`, the mounts, variables, network and GPUs a container is made with), and the contract suite in `resourcetest/` every resource passes, with the Fake in `fakeresource/` (tests only). |
| `internal/resource/gpu/` | The `gpu` resource: GPUs passed with `--gpus`, or by a runtime that reads `NVIDIA_VISIBLE_DEVICES`; its fingerprint is the GPUs' facts. |
| `internal/resource/kubernetes/` | The `kubernetes` resource: a cluster's kubeconfig mounted read-only at `KUBECONFIG`, the network mode that reaches it, and the registry and builder addresses; its fingerprint is the API server and its version, read with `kubectl`. |
| `internal/resource/mounts/` | The `mounts` resource: read-only directories at `/vibesys/mounts/<name>`. |
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
| `internal/production/` | The wiring of the shipped binary: which environments (one docker environment per placement, with its resources), stores and supervisors the daemon runs with. The only package that names a concrete placement kind or resource kind. |
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
- `wire` holds every conversion between `core` and `gen`. Domain types and the generated contract
  types are two separate models; neither is the other's representation. The on-disk encoding is not
  a third model: the files of a store are messages of `store.proto` in protojson, and `filestore`
  translates through `wire` and never imports `encoding/json` (`archcheck` holds that for every
  package under `internal/store/`). The adapters that serve the contract (`daemon`, `supervisor/rpc`,
  `supervisor/host`) import both, because they sit exactly on that boundary and call `wire` for each
  conversion. `cmd/vibesys-cli` and `client` speak the generated messages and import `core` in no
  product file, so they hold no domain rule: how a wait ended, a headline number and a store's
  repository all come from the daemon. They still depend on `core` through `wire` and `gitrepo`,
  which convert at the boundary.
- `gen` imports nothing from this module.
- `lifecycle` imports `core`, `flow` and `schedule` only, so the machine stays pure. `schedule`
  imports `core` only, and a policy `schedule` and `core` only. `lease` imports `core` only, and a
  place of leases `lease`, `core` and what reaches it (`remote`, `clock`); only `production` names
  one, and `app` takes them through a `lease.Book`. `app` imports no transport:
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
- A resource kind's package imports `resource` and `core` (and what it reads a machine with), never
  another kind's, and only `production` imports a concrete kind. A string literal that spells
  `kubernetes`, `slurm` or `nvidia`, in any case, appears only in the package that implements it and
  in the decoding of the operator config (`operator`), so no other code branches on a concrete name.

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
| `--repo` | `VIBESYS_REPO` | A directory in the git repository. Defaults to the current directory. The daemon resolves it, so every linked worktree of a repository, and every directory in one, reaches the same stores. A revision given to a command, such as `HEAD` or `HEAD~1`, is read in the worktree the command runs in when that is a worktree of this repository, and in this directory otherwise. |
| `--task` | `VIBESYS_TASK` | The task the jobs belong to. Required. |
| `--run` | `VIBESYS_RUN` | The id of the optimization run. |
| `--harness` | `VIBESYS_HARNESS` | The agent harness, such as `claude-code`. |
| `--agent` | `VIBESYS_AGENT` | The agent making the call. |

Each `submit` also takes `--placement`, which defaults to `VIBESYS_PLACEMENT`: the placement of the
operator config a docker job runs on (see "The operator config"). Unset, the job runs on the one
placement that gives every resource its task needs, and is refused, naming them, when none or
several do. A host job has no placement: one inherited from `VIBESYS_PLACEMENT` is dropped, and
`--placement` given beside `--env host` is refused, naming the flag. Without `--env`, every job runs
where the task's check and measure run (see "Running in docker"), a command job too.

### Environment

These are all the environment names the CLI reads (through one `getenv`, which a test records).
The orchestrator sets every one of them for the agents it launches but `VIBESYS_PLACEMENT`, from the
golden `proto/vibesys/cli/v1/conformance/identity_env.golden.json`, which the CLI's and the
sidecar's tests both read; a name that is empty there is left unset in the agent's environment.
`VIBESYS_PLACEMENT` is the operator's: an agent inherits it from the environment it was started in.

| Name | Meaning |
| --- | --- |
| `VIBESYS_REPO`, `VIBESYS_TASK`, `VIBESYS_RUN`, `VIBESYS_HARNESS`, `VIBESYS_AGENT` | The defaults of the context flags above. |
| `VIBESYS_PLACEMENT` | The default of `--placement`. |
| `XDG_RUNTIME_DIR` | Where the runtime directory is (see "The daemon and the supervisors"). Ignored unless it is an absolute path. |
| `VIBESYS_CLI_SANDBOX` | `1` makes the daemon a sandbox daemon, beside the one that holds the machine, in the runtime directory of `XDG_RUNTIME_DIR`, which it needs (see "The daemon and the supervisors"). Any other value is refused. |
| `XDG_STATE_HOME` | Where the task index is: `$XDG_STATE_HOME/vibesys`, else `~/.local/state/vibesys`. Ignored unless it is an absolute path. |
| `HOME` | The home directory the index falls back to. |

The orchestrator also sets `VIBESYS_CLI`, the absolute path of this binary for the skills to run
(`"$VIBESYS_CLI"`), and `VIBESYS_SESSION`; the CLI itself reads neither. Installing and upgrading
read their own settings (`VIBESYS_CLI_VERSION`, `VIBESYS_CLI_INSTALL_DIR`, `VIBESYS_CLI_RELEASES_URL`).

| Command | What it does |
| --- | --- |
| `submit check <commit> [flags]` | Queue the task's check. A pass already saved for the commit answers it without running. Also `--wait`, `--note`, `--no-cache`, `--plan` and `--env`. |
| `submit measure <commit> [--samples n] [--recheck] [flags]` | Queue the task's benchmark for `n` samples (the task's default when omitted). Saved samples count: only the missing ones run. Flags as for `check`. |
| `submit command <commit> [flags] -- <argv...>` | Queue a job and print its id. Returns at once; `--wait` blocks as `wait` does. Also `--note`, `--no-cache`, `--plan`, `--env`, `--timeout`, `--service` and `--probe`. A revision is resolved to a full commit id. `--service` and `--probe` are split into words as a shell would split them, with single quotes, double quotes and backslashes, and nothing else of a shell. |
| `wait <job>... [--any] [--timeout d]` | Block until the jobs are final, or with `--any` until one is. The daemon decides how the wait ended and keeps the timeout. |
| `cancel <job> [--reason text]` | Stop a job and print it once it is final. Cancelling a final job prints it unchanged. |
| `note <job> <text>` | Add a note to a job in any state. |
| `jobs [--commit rev] [--state s] [--note-contains text]` | List the task's jobs, oldest first, with why each queued job waits. |
| `logs <job> [--follow]` | Print a job's output, and with `--follow` keep printing until it ends. |
| `show <commit>` | Every result saved for a commit, oldest first: each check, and the commit's measurements pooled as `compare` pools them, one line per environment, task version and metric, naming every measure job it holds (a top-up adds its samples to the same measurement). A measurement's `value` is its headline number, the mean of its samples, computed by the daemon. With `--json`, `results` has each job's result and `measurements` the pooled ones. |
| `compare <commit> [--against rev]` | `better`, `worse`, `within_noise` or `inconclusive`, against the result branch head unless `--against` names a commit. For a multi-objective task, also `tradeoff`, with a verdict on every objective (see "Multi-objective tasks" below). Refuses to mix environments. |
| `promote <commit>` | Fast-forward the result branch to a commit that is `better` than its head, or print every reason it was refused. A refusal exits `1` and is printed on stdout. A promoted multi-objective commit is also offered to the frontier. |
| `result` | The head and a final number measured after the head last moved, or why there is none (exit `1`). |
| `frontier add <commit>` | Add a measured, check-passed commit of a multi-objective task to the task's frontier, evicting the members it beats, or print every reason it was refused. A refusal exits `1` and is printed on stdout; a commit of a single-metric task is `INVALID_INPUT`. |
| `frontier list` | The frontier's members in the order they were added, each with its headline number on every objective, computed by the daemon. |
| `plan create --title t [--body text \| --body-file path]` | Store a plan, open, and print its number. `--body -` and `--body-file -` read standard input. |
| `plan list [--status s] [--search text]` | List the task's plans by number. With no `--status` every plan is listed. |
| `plan view <n>` | Print a plan, its comments and the jobs linked to it (`submit --plan <n>` links one). |
| `plan edit <n> [--title t] [--body text \| --body-file path] [--status s]` | Change what is given and keep the rest. |
| `plan comment <n> --body text \| --body-file path` | Add a comment, which carries a decision or a result. |
| `plan close <n> --status s [--comment text \| --comment-file path]` | Set a status other than `open` and add a comment, as one change. |
| `daemon ensure` | Make sure a compatible daemon answers, starting one if none runs (the start protocol below). It prints `ready`, the pid and the version, or the daemon's status with `--json`. It exits `0` when a daemon answers and `4` when none does, whatever the reason, and the message says why; only a malformed command exits `2`. A program that starts the daemon, such as the orchestrator, runs this and reads the exit code. |
| `daemon status\|stop\|restart` | Report on, stop, or restart the daemon. `daemon status` also says what every job inherits from the daemon: the directory clean checkouts are made in (`checkouts`, from the daemon's `TMPDIR`), and one `env` line per environment variable, sorted, with its value only for `PATH`, `HOME`, `TMPDIR` and a `VIBESYS_` variable whose name has none of `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL` (with `--json`, `environment` and `checkout_directory`). Jobs see the environment the daemon was started with, so a variable set later reaches them only after `daemon restart` from a shell that has it. `daemon status` with no daemon prints `not running` (with `--json`, an `UNAVAILABLE` `Failure`) and exits `4`. `daemon install` is not available yet (exit `2`). |
| `daemon forget <repository>` | Give up a repository that is gone for good, such as a store an older daemon made inside a removed linked worktree: its commands are ended, then its jobs leave the daemon, what is left of their supervisors is let go of, and its stores leave the index. A command that does not end when told is reported left, and the next job waits while it can run, up to its time limit. The answer comes after the supervisor directories of the repository's other jobs are removed, so none is listed after it but the ones reported left. Nothing is touched where the repository was. A repository that is there, one whose directory cannot be read (a disk that is not mounted or does not answer), or one the daemon does not know, is refused: only a repository whose directory is not there is gone. |

Plans belong to a task, are numbered 1, 2, ... by the daemon and never renumbered, and take one of
five statuses: `open`, `chosen`, `won`, `parked` and `rejected`. The CLI orders none of them, so an
agent may move a plan from any status to any other, and it stores the body and the comments as they
are given. A plan number may be written `3` or `#3`. `plan view` shows the jobs linked to a plan. With
`--json`, each linked job carries `verdict`, the daemon's comparison of the job's commit with the
result branch's head (the function `compare` uses), and no `verdict` when there is none to give: no
head, a commit or the head not measured, or measurements from two environments. The text view does not print it, so
run `compare` on a linked job's commit for one. A submission or `plan create` makes a task's store, so a planner can write plans before
the first job. The other plan commands refuse a task with no store with a "no store" error, and
`plan list` prints nothing for it.

Every command ends with one of five exit codes, and which one is a function of one answer: the
command's own yes or no, how a `wait` ended (a `WaitOutcomeKind`), or the kind of a `Failure`.

| Code | Meaning | Endings |
| --- | --- | --- |
| 0 | yes | success; `ALL_SUCCEEDED`, `FIRST_SUCCEEDED` |
| 1 | the answer is no | `SOME_FAILED`, `FIRST_FAILED`; a refused promote or frontier add; no current result; `CONFLICT`, `ENVIRONMENT_MISMATCH`, `NOT_MEASURED` |
| 2 | invalid input | usage errors; `INVALID_INPUT`, `NOT_FOUND` |
| 3 | timed out | `TIMED_OUT` |
| 4 | no answer | `UNAVAILABLE`, `INCOMPATIBLE`, `INTERNAL`, `UNSPECIFIED`; a kind or outcome this build does not know; a wait that could not learn how its jobs ended; no daemon for `daemon status` or `daemon ensure` |

Exit `4` says nothing about the jobs: they may still succeed. A value from a newer daemon that this
build cannot read is `4`, never `0`. `TestTheReadmeExitTableIsTheCode` holds this table to the code.

The daemon decides how a `wait` ended, and the command prints what it sent. A `wait` with a
`--timeout` asks the daemon for it, and the daemon answers `TIMED_OUT` when it passes; no job is
cancelled. A `wait` whose connection to the daemon breaks connects again and asks for what is left
of its timeout. It tries within that time if it has a timeout, and for about a minute since it
last heard anything if not. A timeout that passes while no daemon answers is exit `4`, not `3`,
because no daemon said the jobs were not done.

With `--json`, a command prints the contract's messages in protobuf's canonical JSON form with the
original field names, one compact message per line. That is the response for `submit`, `cancel`,
`note`, `jobs`, `logs`, `show`, `compare`, `promote`, `result`, `daemon status` and the `plan` and
`frontier` subcommands. For `wait` and `submit --wait`, it is the daemon's `WaitOutcome`: its `kind`, the job
that decided it in `decided_by`, and every job's last state. An error that a command reports is a
`Failure` on stderr: a refusal from the daemon, a missing argument, a job that does not exist, or a
`wait` that gave up. The `Failure` the daemon sent is printed as it was sent, a kind this build does
not know included.

Some errors are plain text on stderr even with `--json`, because they come before the command has
read its flags: a flag that does not parse (exit `2`, printed by Go's `flag` package), and an
unknown command or none (the usage text, exit `2`). A caller reads the exit code first, and parses
stderr as a `Failure` only for `1`, `2` and `4`, and only when it parses.

Text output rounds numbers to four significant digits. `--json` and the verdicts behind it carry
the exact values.

A signal ends the command and nothing more. A job the command submitted or waits on keeps running.

## Who submitted a job

A job records every caller that submitted it in `Job.submitters`: one entry for each distinct
`(run_id, agent)`, oldest first, each with the time of that caller's first submission. The entry
comes from the call's `VIBESYS_RUN` and `VIBESYS_AGENT`; a person at a terminal names neither, and
their entry has an empty run and agent. A submission that joins a queued or running job, or is
answered from a finished job's saved result, adds its caller to that job and changes nothing else,
so a final job gains a submitter without changing state. A caller already on the job adds nothing.

The store retains protojson format version 2. Existing records load without rewriting, and
historical jobs keep an empty submitter list rather than invented identities. Upgrade all writers:
an older writer rejects the unknown field in a job that has submitters. No store deletion is needed.

## The daemon and the supervisors

There is one daemon per user per machine. Its process name is `vibesysd`; it runs from the `vibesys-cli` binary. It listens on a Unix socket in a private (`0700`) runtime
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

The runtime directory moves with `XDG_RUNTIME_DIR`, so its lock alone cannot keep one daemon per
user per machine. The daemon also holds `machine.lock` in the user's machine directory, which
comes from the user id and the machine and from no variable: `/var/tmp/vibesys-cli-<uid>` when
`/var/tmp` is on local disk, and otherwise `/run/user/<uid>/vibesys-cli-machine`, the user's
memory-backed directory, as on a machine whose root is on a network file system (one with neither
refuses to start a daemon). It is private (`0700`) and on local disk, as the runtime directory is. The lock file says which daemon holds it:
its pid, runtime directory and version. A daemon of another runtime directory is refused, naming
that one, so `daemon ensure` and every command that starts a daemon fail with the holder in the
message. The operating system drops the lock when the daemon dies, however it dies.

A sandbox daemon is the one way to run a second daemon on purpose, for a test or a second checkout:
set `VIBESYS_CLI_SANDBOX=1` and an `XDG_RUNTIME_DIR` of its own (not the one of the daemon that holds
the machine, whose socket its commands would reach). It holds no machine lock; it records itself
under `sandboxes/` in the machine directory for as long as it runs, and `daemon status` of every
daemon lists the sandbox daemons that run (`sandbox  pid  runtime  version`), with `role  sandbox`
on the sandbox's own. Its containers and Slurm jobs carry the scope of its own runtime directory. A
test that starts a daemon in-process holds no machine lock either.

A command that needs the daemon connects and, if none runs, starts one and waits for its readiness
signal. A daemon that fails to start closes the signal without it, so the client reports the failure
at once and does not wait out a timeout. The client and the daemon exchange their versions when they
connect, and a mismatch is a refusal that says which side is older and what to run: `daemon restart` for an old daemon, an upgrade for an old client.

`daemon ensure` is that same start protocol as a command: it holds `start.lock`, so any number of
`ensure` commands and other commands that run at once start one daemon between them. A new client
replaces a daemon older than protocol 3 (`MinDaemonProtocolVersion`), because only a daemon of
protocol 3 can serve `WatchTask`. The entire ensure operation has a two-minute limit,
including the start-lock wait, handshakes, stop, child readiness and final status. Expiry
releases startup ownership and returns a typed unavailable failure with exit 4.

### Watching a task

`WatchTask` is a server-streaming call that tells a program everything it needs to show a task:
the head of the result branch, plans, promotions, jobs and their results, a judgment of each of the
newest measured commits (`judged_commits`, 0 for 20, at most 100), jobs that are held up and why,
and whether the task's store is readable. The state is a set of keyed facts. On every wake the
daemon captures the store under one request lock, checks its revision and lifecycle view after
deriving branch and trouble facts, and sends the facts that differ from what the stream last sent, so a
wake missed or merged with another loses nothing. Wakes are store commits (including failed ones),
fast-forwards of the result branch, new jobs, changes in what holds a job up, and the slow period.

- A stream starts with a reset: `Reset`, the whole state in messages of at most 500 facts, then
  `CaughtUp`. A client holds a reset aside until `CaughtUp`, so it never shows half of one.
  A later reset is sent when a fact stopped existing, since a client cannot be told a fact is gone,
  or when more than 500 facts changed at once. A judgment that leaves the judged commits is dropped
  without a reset.
- `sequence` counts the messages of one stream from 1, with no gaps. Protocol 3 ignores the resume
  token: a new stream starts with a reset.
- A store that is away changes only the health fact; what was sent stays true until it is read again.
- `Hello` takes a component, `COMPONENT_CLI` or `COMPONENT_ORCHESTRATOR`; an unknown one is
  `INVALID_INPUT`, and a client of a protocol older than the daemon's minimum is told to upgrade.

`proto/vibesys/cli/v1/conformance/` holds the files a client of the daemon and the daemon agree on:
the protocol number that added `WatchTask`, where the daemon's socket is for an environment, and
transcripts of `WatchTask`. This module replays them against the real server, so a second reader
cannot drift from it.

Each running job has its own supervisor process. It owns the job's process group and keeps the
job's output, so it outlives the daemon. The daemon takes each decision from `lifecycle`, and which
queued jobs start from `schedule`:

- A job holds claims from its launch until its end is recorded. A docker job claims a slot of
  `placement:<name>`, the placement it runs on, which has the placement's `max_jobs` slots, and
  what the placement's resources claim: `kubernetes:<machine><kubeconfig>` for a cluster and
  `gpu:<machine>/<devices>` for GPUs, which every placement that gives the same one shares. A host
  job claims a slot of `placement:default` when that placement is local, and otherwise of `host`,
  one slot of its own. A measure job claims each of them whole, so a benchmark sample never runs
  beside another job of its placement or of another placement on its cluster or GPUs, while the
  jobs of other placements run. A command job's placement is decided when it is submitted, as a
  check's is.
- No resource is given past its capacity and an exclusive claim shares its resource with nothing,
  whatever the policy. The policy (`[scheduler] policy`) only orders the jobs that may start:
  `fifo` takes the oldest first, and `fair-share` takes the next job of the run (the run that first
  submitted it) that used least of what the job claims lately. A job that waits keeps its turn: a
  later job does not take what an earlier one waits for, so a measure is not starved by checks.
- A job whose supervisor is parked goes first. A job in doubt holds its claims while its command
  can run; one whose work the daemon could not read holds every resource. Recovery adopts running
  jobs with their claims, even past a smaller capacity, and starts nothing beside them that does
  not fit. A job whose claims can never be granted stays queued, and a wait on it ends with why.
- With the defaults the daemon runs one job at a time, the oldest queued first, as before
  schedulers. A change of policy or `max_jobs` takes a `daemon restart` and nothing else: what the
  scheduler knows is derived from the jobs.

The scheduler of one daemon keeps its own jobs apart. A lease keeps the jobs of every daemon apart:
other users' daemons on the same machine, and daemons on other machines that share an ssh host.

- Before a job that holds a resource alone starts, the daemon takes that resource's lease where
  every daemon sharing it looks. A placement's slots (`placement:<name>`) are leased as the machine
  its engine runs on: for a `local` placement, a `flock` on `machine.lock` in this machine's
  directory of leases; for an `ssh` placement, the same file on the engine's machine, held by a
  shell there over ssh. So the measures of every placement on one machine, of
  one daemon or of several, take turns. Host jobs that claim a slot of their own (`host`) take this
  machine's lease. A Kubernetes cluster a placement gives is leased in the cluster (below). GPUs
  take no lease of their own, since their machine's lease covers them, and a `slurm` placement takes
  none, since Slurm's exclusive allocation arbitrates. A claim that shares its resource takes no
  lease.
- The directory of leases is `vibesys-leases` in the first of `/run/lock`, `/var/tmp` and
  `/dev/shm` that is sticky, writable by every user (mode 1777) and on local disk. It is made sticky
  and writable by every user when it is not there. A parent on a network file system (judged as
  the runtime directory is) is passed over: a lock there can outlive its crashed holder until the
  server lets it go, and a directory the network shares is shared with other machines, whose leases
  of the same names are other things. The order depends on the machine alone, never on the user or
  the environment, so every user's daemon finds the same directory, and a machine reached over ssh
  finds its own the same way, by `stat` there. A machine with none of them refuses to start the
  daemon with an error that names each parent and why it was passed over.
- A lease another holds keeps the job queued, starting nothing. The job keeps its turn, so the
  daemon's shared jobs wait behind it and do not disturb the other's measure. `jobs` names the
  holder: `waiting: needs placement:default alone, leased by bob on host-a (pid 4242), job 7f3a`.
  The job tries again every slow period (30 seconds).
- A job's leases are held exactly as long as it holds its claims, and are given back on every way
  it ends: it succeeds, fails, is cancelled, cannot launch, or is given up. A daemon that stops
  gives back its leases, and a daemon that dies loses them: the kernel closes its locks and its ssh
  connections. The next daemon takes the leases of the jobs it adopts again.
- A job's command outlives its daemon, but its lease does not. So a measure whose daemon dies runs
  unleased until the next daemon adopts it. If another daemon took the lease in between, the
  measure goes on, and `daemon status` reports the conflict until it ends.
- `daemon status` lists the leases the daemon's jobs hold, the conflicts, and the other `vibesysd`
  processes of the machine, of every user.

A `Kubernetes` lease (`internal/lease/kubelease`) is a `Lease` object in the cluster, renewed by
its holder and taken over once it expires, so a holder that died blocks the cluster for at most a
duration (60 seconds). The daemons judge expiry by their own clocks, which must agree to well within
the duration. The daemon writes the lease `vibesys-cluster` in the `default` namespace with
`kubectl` on the engine's machine, through the placement's kubeconfig, as the Kubernetes resource
reads the cluster, so the daemons of every machine whose placements give one cluster take turns at
it.

`jobs` prints why each queued job waits, under its line:

```text
j7	queued	0123456789ab	measure (0 samples)
	  waiting: needs placement:default alone, in use by j5 (check, run run-a), j6 (check, run run-b)
j8	queued	0123456789ab	check
	  waiting: behind j7 (measure, run run-b), which waits for placement:default
```

A job of another task is named with its task, and with `--json` the reasons are in `waits`.

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
- A running job whose supervisor is proved gone (the machine rebooted, or the supervisor was
  killed) is cut off, with one explanatory line in its output. The run's record says whether the
  daemon had told the supervisor to start the command. A job whose command never started goes
  back to the queue in its old place. A check or a measure job also goes back after its command
  may have started, as rerunning one only measures again. A command job whose command may have
  started becomes `lost`: it may have had effects, so it is never run twice. A job gets
  `MaxAttempts` (3) attempts; the job keeps each attempt that was cut off, and the last one cut
  off makes it `lost`. A job that ran and failed or ran out of time is never run again, and a
  supervisor that cannot be judged dead holds its job in doubt rather than running it again.
- A supervisor that was started for a job the store never marked running is discarded.
- A store that cannot be read, such as a repository on a disk that is not mounted, stays known and
  is read again every slow period. None of its jobs starts, and a run of it is kept, not released.
  A run that is parked or finished runs nothing and holds up no other repository. A command that
  may still run holds the machine for as long as its job may run and no longer. A `wait` on a job
  held up this way ends with why (exit `4`) rather than waiting in silence.
- A running job with no sign of a run here, or with a run this build cannot use, is held in doubt,
  and so is every job of its store after it: no sign of a run is no proof that the command ended,
  since it may run on another machine that has the repository on a shared disk. When the place that
  ran it is gone for good, remove the store's owner record (`.vibesys-state/<task>/.owner` in the
  repository), as the daemon's message and a refused start both say. The next daemon to open the
  store gives up the runs of the jobs it has running: one that no run of can be found here is
  `lost`, at once with no run here and after its time limit with one this build cannot use. A run of
  it that the daemon does find is its own, and the job goes on.
- A store of a version this build does not read, such as one a prerelease build wrote, is kept
  known and refused in the same way, and every other repository works. The daemon logs why, and a
  command on that store fails with `INCOMPATIBLE` naming the file; for the prerelease encoding it
  names the store's directory and its index entry to remove, after which the next submission makes
  the store again.
- A repository that is gone for good is given up with `daemon forget`.

Each task's store is a directory under `.vibesys-state/` in its repository. The daemon writes a
`.gitignore` of `*` into `.vibesys-state/` when it makes it, and into one an older build made when it
next opens a store there, so `git add -A` in a repository or a candidate never commits a store. A
`.gitignore` already there is left as it is.

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

Each command of a job gets its own artifacts directory, numbered in the order the commands ran
(`01-check`, `02-benchmark`, ...), in the repository's state directory
(`.vibesys-state/.artifacts/<task>/<job>/`); the job's output names it before the command runs. A
command finds it in `VIBESYS_ARTIFACTS_DIR`. A benchmark that exits non-zero after writing an error
record says the record's message in the job's reason.

`--no-cache` runs the whole job again, with a fresh check. `--recheck` (measure) checks again but
still takes only the missing samples. A cancelled job, a failed check, and a benchmark that wrote no
usable result save nothing, so a result is never saved without its samples and its check.

A task is read from `.vibesys/tasks/<task>/` of the job's commit, not from the working tree, and its
version (a digest of its files) is part of the cache key. Generated files do not count toward the
version: a `__pycache__` or `.pytest_cache` directory and any `*.pyc` or `*.pyo` file under the task
(`task.Generated`), so bytecode committed by accident never makes two commits incomparable. Once the
task's result branch exists (below), a `submit check` or `submit measure` of a commit whose task
files differ from the branch head's is refused before any job exists, as a `conflict` failure that
names the changed paths: a candidate scores under the task it was given, never one it edited. A benchmark reports its score in one of two
ways: it writes a JSON file at the path given after the flag in `[benchmark.result]`, or, with
`result_protocol = 2` in `[benchmark]`, a stream of evaluator records at the path given after
`--vs-output`. Tasks that declare an `[evaluator]` or an `entrypoint` are refused: their commands
belong to an evaluator package. `${PROJECT_ROOT}` in a command is the checkout's root.

The lifecycle commands above work end to end against a `fakeenv` in tests, and the check and measure
flows run on the same fake in `app` and `daemontest`, so a test of the whole daemon needs no
process. `show`, `compare`, `promote` and `result` read what the stores hold, so they work on results
saved by any means.

### Running in docker

A task with a `Dockerfile` next to its manifest runs its jobs, command jobs included, in the image
that file builds, unless the submission says `--env host`. Nothing falls back: a machine with no reachable Docker daemon
fails the job with a message that says so, and a task that needs what the image cannot give is
refused with the reason.

- The image is built from the task's directory as the job's commit has it: the directory is the
  build context, so the Dockerfile can `COPY` the task's scripts into the image, and the build sees
  no file of the candidate. The image is tagged by a digest of those files, so it is built again
  exactly when a file of the task changes. The first `submit` of a task builds the image before the
  job is queued, so that submission takes as long as the build. The image digest is recorded with
  each result and is part of what makes two results comparable, so results from different images
  are never mixed.
- Commands run in a container as the invoking user, in a clean checkout of the exact commit. The
  checkout and the job's scratch directory are mounted at their own paths, so a path a command is
  given means the same inside. A task with `docker_in_docker = true` is refused: the container has
  no container runtime. A task for the `cuda` backend runs when its placement has a `gpu`
  resource, and is refused otherwise; other accelerator backends are refused.
- A container also gets the cache directory of the task's image at `/vibesys/cache`, kept across
  commits and jobs, and an empty `/vibesys/artifacts` that is kept with the job; the variables
  `VIBESYS_CACHE_DIR` and `VIBESYS_ARTIFACTS_DIR` name them. Why the task environment is shaped this
  way is in [docs/contributing/cli-task-environment.md](../docs/contributing/cli-task-environment.md).
- The container runs on a placement of the operator config and gets what each of its resources
  attaches:

  | Resource | What the container gets |
  | --- | --- |
  | `gpu` | The GPUs, by `--gpus`, or by the runtime with `NVIDIA_VISIBLE_DEVICES`. |
  | `kubernetes` | The kubeconfig, read-only, at `/vibesys/kubeconfig`, with `KUBECONFIG` naming it; the network mode (`docker run --network`), such as `host` for a cluster whose API server listens on the machine's loopback address; `VIBESYS_K8S_REGISTRY` and `VIBESYS_K8S_BUILDER` when the config names them. The task deploys to the cluster and cleans up after itself; the daemon does not manage the cluster. |
  | `mounts` | Each directory, read-only, at `/vibesys/mounts/<name>`, with `VIBESYS_MOUNTS` listing the names. |

  A task names the resources its jobs need in its manifest, `[environment] needs = ["kubernetes"]`
  (any of `gpu`, `kubernetes`, `mounts`), and a job on a placement that lacks one is refused before
  anything is built, with `placement "<name>" has no resource "<kind>"`.
- A result's environment fingerprint pins the placement (`placement`) and what identifies each
  resource: the GPUs' model, count and driver, the cluster's server and version
  (`kubernetes_server`, `kubernetes_version`), the mounts' names. Results from
  different placements or resources are never compared, and a saved result or check pass is reused
  only on the same ones.
- Every container is named and labelled for the daemon that started it. A cancelled or timed out
  job removes its container, and the next job removes any container an earlier job left, for
  instance when the supervisor was killed. Containers that other tools started are never touched.
- The docker program must be on the `PATH` of the daemon, and the user must be allowed to use it,
  on the machine the operator config places the engine on.

### Stopping a command

When a job is cancelled or reaches its time limit, the CLI first sends SIGTERM and then, after a
grace period, kills what is left, so that a task's trusted wrapper can clean up what it made outside
the machine, such as a Kubernetes namespace. On the host SIGTERM goes to the command's whole process
group; in a container it goes to every process but the container's first, which only waits. The
run then waits until the commands have ended or the grace period (`stop_grace_seconds`, 30 seconds
by default) is over, whichever is first, and then ends the group with SIGKILL on the host and removes
the container with `docker rm --force` as before. This holds for a command and for its `--service`
(both are asked at once), for the docker and the host environments, and for the ssh and slurm
placements, whose containers are reached through the same docker calls; a slurm allocation is
cancelled only after the container is removed. Output written while the commands clean up is kept in
the job's log. The grace is spent after the time limit, not inside it, and the largest grace is
shorter than the daemon's five minutes' wait for a stopped job to end.

What it does not do: every process of the group gets SIGTERM, so a helper the wrapper starts for
its cleanup (such as `kubectl`) is asked to stop too, unless it handles or ignores the signal, as a
wrapper does with `trap '' TERM` before it starts the helper. Nothing sends SIGTERM when the supervisor or the daemon is killed: the commands are then
killed at once, as the guard of the host environment and the container's removal always did, and a
probe of a service's readiness is killed at once and is not asked to terminate.

### The operator config

Settings about the machine belong to whoever runs the CLI, never to a task. They are read from
`$XDG_CONFIG_HOME/vibesys/cli.toml` (`~/.config/vibesys/cli.toml`), or the file
`VIBESYS_CLI_CONFIG` names, when the daemon and each supervisor start; `daemon restart` applies a
change. The schema is `OperatorConfig` in `proto/vibesys/cli/v1/operator.proto`; unknown keys and
values its rules refuse are refused, naming the key. With no file, there is one placement,
`default`, of kind `local`, with no resource.

A file names its placements, each a table of its own:

```toml
[placements.local]
kind = "local"
max_jobs = 1
[placements.local.resources.kubernetes]
kubeconfig = "/home/me/.kube/kind-config"
registry = "localhost:5001"
builder = "tcp://127.0.0.1:1234"
network = "host"

[placements.a100]
kind = "slurm"
[placements.a100.remote]
host = "host-a"
remote_workspace_root = "/scratch/me/vibesys-cli"
[placements.a100.slurm]
partition = "a100"
gres = "gpu:1"
[placements.a100.resources.gpu]
devices = "all"
```

| Key | Meaning |
| --- | --- |
| `[scheduler] policy` | Which job starts next: `fifo` (default) or `fair-share`. |
| `stop_grace_seconds` | How long a job's commands get to clean up after a cancel or the time limit asks them to terminate, before they are killed, on the host and on every placement (30 by default, at most 240). See "Stopping a command". |
| `[scheduler] max_jobs` | In a file of the earlier shape only: the `max_jobs` of its one placement. |
| `[placements.<name>] kind` | `local` (this machine's docker), `ssh` (docker runs on `remote.host`, reached over ssh) or `slurm` (as `ssh`, with every container and image build inside a new allocation made by `srun`). A name is lower-case letters, digits, `_` and `-`. |
| `[placements.<name>] max_jobs` | How many jobs may run on the placement at once (1 by default). A measure job runs alone on it. More than 1 needs kind `local`, since another machine stages and sweeps one job at a time. |
| `[placements.<name>.remote] host`, `ssh_command`, `rsync_command` | For `ssh` and `slurm`: the ssh destination and the programs that reach it. |
| `[placements.<name>.remote] remote_workspace_root` | An absolute directory there for the copies of runs' directories (`staged/`) and the caches (`cache/`). |
| `[placements.<name>.slurm] partition`, `gres`, `shared_gres`, `exclusive`, `cpus`, `memory`, `srun_arguments` | For `slurm`: what an allocation asks for, by what it runs: an image build asks for `cpus` (8 by default) and `memory` only, and builds in a BuildKit builder confined to them; a check asks for `shared_gres` (such as `shard:1` of a GPU Slurm shares; `gres` when unset) and the same share of the node; a benchmark sample asks for `gres` and, with `exclusive` (true by default), the whole node. Every container is confined to its allocation's CPUs. |
| `[placements.<name>.slurm] poll_interval_seconds`, `queue_timeout_seconds` | The pause between reads of a job's state while its cancel is confirmed, and the bound on the wait for an allocation (24 hours by default). |
| `[placements.<name>.resources.gpu] devices`, `runtime` | A `docker --gpus` value such as `all`; with `runtime` (such as `nvidia`) the GPUs are passed by that runtime and `NVIDIA_VISIBLE_DEVICES` instead of `--gpus`. |
| `[placements.<name>.resources.kubernetes] kubeconfig`, `registry`, `builder`, `network` | An absolute path of the kubeconfig on the engine's machine (required), the registry and image builder addresses the container gets, and its network mode (empty leaves docker's default). |
| `[placements.<name>.resources.mounts] <name> = "<dir>"` | A directory of the engine's machine every container sees read-only at `/vibesys/mounts/<name>`. |

A file of the earlier shape is read as the one placement `default`, and needs no edit: `[docker]
placement` is its kind, `[remote]` and `[slurm]` its sections, `[docker] gpus` and `runtime` its
`gpu` resource and `[docker.mounts]` its `mounts` resource. A file holds one shape or the other.
Each placement's containers have a scope of their own, and those of `default` keep the scope the
daemon's one placement had.
On another machine, a run's checkout and scratch directory are copied to `remote_workspace_root`
and mounted at their local paths; the scratch directory and the artifacts are copied back when the
command ends, and a copy that fails makes the command's ending unknown. Under `slurm`, a container
is removed and then its allocation is cancelled and confirmed ended, on every path, because the
Docker daemon runs outside Slurm's cgroups; a run first cancels any allocation of its scope that a
dead run left. The queue wait does not count against a command's timeout. The ssh host must be the
node Docker runs on.

A docker fingerprint holds the placement and its resources' facts, so a docker result saved by a
build before placements is not compared with, or reused for, one of this build.

A sandbox daemon (see "The daemon and the supervisors") also takes its own `XDG_STATE_HOME` and
`VIBESYS_CLI_CONFIG` when it should not share the first daemon's index or operator config. Stores
live in the repository, so it must use another clone of a repository the first one uses.

## The result branch

Each task has a result branch, `vibesys/result/<task>`, in the repository the command runs in. Create
it at the commit the run starts from (`git branch vibesys/result/<task> <commit>`); a command that
needs it and does not find it says so. It must not be checked out. Only `promote` moves it, and only
forward: the new head must contain the old one, which a commit that does not is told as a
`conflict` failure.

`promote` decides from the saved measurements alone (internal/core), in this order: it is refused
with every reason that applies (`unmeasured`, `check_missing`, `worse`, `within_noise`,
`inconclusive`, `tradeoff`, `stale`, `environment_mismatch`), or it approves, which is a `core.Approved` that
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

## Multi-objective tasks

A task is multi-objective (Pareto) when its `objectives.toml` has a `[pareto]` section and declares
more than one objective. It must report its score with `result_protocol = 2`, so that every sample
reports every objective; a sample that leaves one out ends the measure job `failed` and saves
nothing. Each saved measurement records the objectives, so `compare`, `promote` and `frontier`
decide from what was saved. Every other task is judged on its one metric, exactly as before.

`compare` judges each objective with the same statistic as a single metric, under the `[pareto]`
`relative_noise` as the noise floor (none declared is Welch's t test), and combines the verdicts.
An objective may declare a floor of its own, which replaces the `[pareto]` one for that objective
only, because objectives of one task differ in spread (throughput near 3%, p99 latency 10 to 19%):

```toml
[[objective]]
name = "p99_ms"
direction = "min"
relative_noise = 0.15        # optional; overrides [pareto] relative_noise for this objective

[pareto]
relative_noise = 0.03        # the floor of every objective that declares none
```

The value is at least 0 and less than 1 (`0` declares no floor for that objective, so Welch's t
test judges it); anything else, or a `relative_noise` in a task that is not multi-objective, is
refused naming the objective. The floor is in `objectives.toml`, so it is part of the task version,
and each measurement records it with its objectives: measurements judged under different floors
are not pooled. A task with no per-objective value is judged exactly as before.

| Overall | When |
| --- | --- |
| `inconclusive` | Any objective is inconclusive; the reason is the first such objective's. |
| `better` | Better on some objective and worse on none. |
| `worse` | Worse on some objective and better on none. |
| `tradeoff` | Better on one objective and worse on another. |
| `within_noise` | Within noise on every objective. |

With `--json`, `comparison.verdict` holds the overall `kind`, `reason` and sample counts, with its
means, ratio and noise left at zero, and `comparison.objectives` holds one `{metric, verdict}` per
objective in the order the task declares them, each verdict with its own numbers. `promote` moves
the branch only on an overall `better`, and refuses a `tradeoff` with the reason `tradeoff`; its
refusal reasons and its promotion carry the same `objectives`.

The frontier is the set of the task's commits that no member beats. `frontier add` judges the
commit against each member with the comparison `compare` makes: it evicts a member it is `better`
than, keeps one it is a `tradeoff` against, and is refused with every reason that applies, each
naming the `member` it is about: `unmeasured`, `check_missing`, `dominated` (the member is better),
`within_noise`, `inconclusive`, `stale` (measured under another task version) and
`environment_mismatch`. A commit that is a member already is answered `already_member` and nothing
changes. The JSON answer is `decision.admitted` (`added`, `evicted`, `already_member`, `members`)
or `decision.refused.reasons`. A successful `promote` of a multi-objective commit offers it to the
frontier in the same way, and its answer's `frontier` field says what the frontier made of it; the
promotion stands even when the frontier refuses the commit. The frontier takes the commit before
the promotion is recorded, so a crash between the two leaves a frontier that is right, and the
promotion can be asked for again. `frontier list` prints `members`, each a `member` (`commit`,
`added_at`) and its `values`: per objective, the `metric`, the mean of every sample of the commit
(`value`) and the number of `samples`.

## Contract

The contract is `proto/vibesys/cli/v1/`: the daemon's gRPC service, the job, result, verdict,
environment, event, plan and frontier messages, typed refusals, and the version handshake. The same messages
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
  its waiters are told why, and that no repository going away fails a job. A profile of several slots
  runs jobs of several runs beside each other, some needing the machine alone as a measure does,
  through crashes and restarts, and checks that no more commands run than slots and that one
  needing the machine alone runs beside nothing.
- `internal/lifecycle/model_test.go` checks the machine exhaustively for two jobs: every order of
  events up to a depth, for safety (one command at a time, nothing started beside a job its store
  shows running), the cancel a user asked for, and that a healthy world ends every job. With more
  than one slot, generated event sequences and restarts check every job's claims after each event.
- `internal/schedule/scheduletest` holds every policy, by property over generated states, to the
  scheduler's rules: capacity, exclusivity, a decision and a true reason for every job, determinism,
  every job starting once the holders end, a waiting job keeping its turn, a job refused a lease
  waiting for it, and for `fair-share` no run starved. A test feeds it broken schedulers and checks
  that each rule catches its own.
- `internal/lease/leasetest` holds every place of leases to one holder at a time, the holder named
  to whoever is refused, keys apart, a release on every path that never gives back a later holder's
  lease, a crashed holder's lease given back, a lost lease saying so, and a random model of three
  daemons. The fake, this machine's flock, the ssh host's shell (run over the local shell) and the
  cluster's `Lease` (over a fake API server) pass it, and a test feeds it broken leases. Two real
  processes take turns at a flock, one killed with `SIGKILL`. With `-tags kind` and
  `VIBESYS_KIND_KUBECONFIG` (and `VIBESYS_KIND_CONTEXT`) naming a cluster, such as a kind one, the
  `Lease` objects pass it against a real API server.
- `internal/sim/leases_test.go` runs two users' daemons on one machine through crashes and restarts,
  sharing only the machine's lease, and checks that their measures never run at once, except where
  the daemon of one died while it ran; a test with the lease not shared sees them meet.
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
