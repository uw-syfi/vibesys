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
install in place. If no stable release is published yet, or the pinned version is not, the script
says so and installs nothing; it never falls back to a pre-release.

| Setting | Effect |
| --- | --- |
| `VIBESYS_CLI_VERSION=0.2.0` | Install that version instead of the newest. |
| `VIBESYS_CLI_INSTALL_DIR=/path` | Install there instead of `~/.local/bin`. |
| `VIBESYS_CLI_RELEASES_URL=https://host/path` | Read release assets from a mirror. The default is `https://github.com/uw-syfi/vibesys/releases/download`. |

Check what you installed, and the task schema versions it reads:

```sh
vibesys-cli --version    # vibesys-cli v0.x.y (task schemas 0.1, 0.2)
vibesys-cli --version --json   # the same build as a BuildVersions: binary_version and the protocol it speaks
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
`install.sh` into a directory you own. If no stable release is published yet, the command says so
and suggests pinning a version with `install.sh`. A development build reports version `dev` and upgrades to
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
| `internal/store/` | `Store`: the logical interface to results, events, logs, job state, plans, declared bounds, promotions and the frontier. No paths, no formats. |
| `internal/store/ledger/` | The decisions every `Store` shares (what a submission joins or reuses, numbering, what a value may hold), as a pure `Ledger`, and the `Engine` that runs one over a `Backend`. It asks `core` whether a transition is legal (`Job.Apply`) and whether a submission joins a job (`CanJoin`). |
| `internal/store/memstore/` | In-memory `Store` for tests. The same `Engine` as `filestore`, with a backend in memory. |
| `internal/store/filestore/` | The only code that knows the on-disk layout. Every file is protojson of a message in `store.proto`, so no hand-kept record format exists. A store of format version 2 to 6 is migrated to version 7 when it is opened (version 3 only added the frontier and fields that a version 2 program would refuse, version 4 a job's placement, version 5 a job's needs and task version, and version 6 a plan's origin, transitions and workstream with the `bounds` and `workstreams` directories, so that a program of version 5 refuses the store and never opens it to ignore them, and version 7 a job's standing allocation, which a program of version 6 would refuse as an unknown field); a docker result of version 4 or older is keyed again when it is read, as where it ran left the cache key in version 5 (see "Placements are interchangeable"); a store of any other version is refused. Its package comment states the layout, the commit protocol and the filesystem operations it relies on. |
| `internal/store/filestore/fsys/` | The slice of a filesystem `filestore` uses, the operating system's implementation, and an in-memory one that models what a crash keeps and fails or dies at any write step. |
| `internal/store/storetest/` | The one contract suite every `Store` passes, and a differential driver that compares stores. |
| `internal/environment/` | `Environment`: where a job's commands run (`host`, `docker`), and `Registry`, the environments a build has by kind. `Allocator`: how a placement's standing allocations are made, observed, released and found again after a restart. |
| `internal/environment/fakealloc/`, `allocatortest/` | A `FakeAllocator` that keeps a backend of standing allocations in memory (a queue, time limits by a clock the test moves, cancels from outside, unreachable windows), and the one contract suite every `Allocator` passes. Tests only. |
| `internal/environment/host/` | The host `Environment`: runs a command on this machine in its own process group, and ends the whole group on every path. Passes the environment contract suite. |
| `internal/backend/` | `Backend`: facts about the compute hardware a task targets. |
| `internal/backend/cpu/` | The CPU `Backend`: the machine's facts and load, read through a `System` seam with a fake. |
| `internal/task/` | Reading a task from `.vibesys/tasks/<task>/` as a commit has it (unknown keys, `[evaluator]` and `entrypoint` tasks are refused), its version, and `Source`, how a task is fetched. |
| `internal/reading/` | Reading a benchmark's result: one field of a JSON file, or the records of an evaluator result stream. |
| `internal/flow/` | The check and measure flows as pure step functions (`State`, event in, `State` and requests out), and the `Plan` a job is run from. No clock, goroutines or files. |
| `internal/flowrun/` | The shell that carries a flow's requests out on an `Environment`, a `Backend` and a file system, and nothing else. A `Fake` environment passes the same scenarios as the real one. |
| `internal/hostfs/` | The file system a job reads results from, with the operating system's and an in-memory one. |
| `internal/gitwork/` | The git program as a source of tasks (`git archive`) and of clean checkouts of a commit (`git clone`, then a detached checkout). The working tree of the repository is never read or touched. |
| `internal/alloc/` | Standing allocations as a pure machine: `Step(State, Config, now, Event) (State, []Action)` and `Fold` over a log of them. It holds the states (pending, running, draining, ended), the fit of a job (`Fit`), where a job of any allocation goes (`Choose`), the renewal rule, max_held, and typed refusals. The daemon runs it (`internal/app`). |
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
| `internal/proctable/` | The machine's process table for tests that run real processes: the processes that carry a variable under a directory, and their descendants, and whether a process still runs. Linux reads `/proc`, macOS asks the kernel with `sysctl`, and a table that cannot be read is an error, never an empty answer. Tests only. |
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
| `internal/environment/apptainer/` | The apptainer placement: an `Engine` that places the docker environment's containers on a Slurm cluster whose nodes run Apptainer and no Docker. It converts each image to a SIF on this machine with its apptainer program and copies the SIF to the cluster once, by the digest of the image's content, runs a run's containers as steps of one batch allocation, and reads the machine and the GPUs on the node. It decides over a `Cluster`, the narrow interface to the cluster. |
| `internal/environment/apptainer/clustercli/` | The `Cluster` that runs Slurm's and Apptainer's programs through ssh on the login node. Its tests run it against fake `srun`, `apptainer` and `scancel` programs. |
| `internal/environment/apptainer/clusterfake/` | A `Cluster` that keeps a cluster in memory, with queued jobs, nodes that run the fake world's scripts, and faults, and a `Converter` that builds SIFs in memory. Tests only. |
| `internal/environment/apptainer/localsif/` | The `Converter` that runs this machine's `apptainer build`, found on the daemon's `PATH`, in a scratch directory of its own. Its tests run it against a fake `apptainer` program. |
| `internal/remote/` | Running programs on a machine and copying directories to and from it: this one, or another over ssh and rsync. An exit code that is ssh's own is never taken for the program's. |
| `internal/backend/nvidia/` | The GPU facts of a machine (model, count, driver, the driver's CUDA version) for a fingerprint, read with `nvidia-smi`. |
| `internal/backend/amd/` | The AMD GPU facts of a machine (model, count, the amdgpu driver's version) for a fingerprint, read with `rocminfo` or `amd-smi` and `/sys/module/amdgpu/version`. |
| `internal/resource/rocm/` | The `rocm` resource: the machine's AMD GPUs, and a check, run in the container before the command, that the image's ROCm runtime sees them. |
| `internal/tomlproto/` | Decoding TOML into a proto message by its field names, refusing an unknown key by name, and a key newer than the schema version the file declares (`task_schema_since_minor`). The task manifest and the operator config are read with it. |
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
| `internal/archcheck/` | The layer, layout, clock, goroutine, `/proc` and fuzzing rules below, as a check `go test` runs. |
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
- A test file that names a path under `/proc` is Linux-only (a `_linux` name or a `//go:build`
  constraint that keeps it off macOS) or is listed in `archcheck` with the reason it runs where
  `/proc` is missing. macOS has no `/proc`, so a test that read it for processes found none there and
  passed or failed for the wrong reason, and left the daemons it was to end running. A test that
  needs the processes of a machine reads them through `internal/proctable`.

`internal/archcheck` enforces all of these. A new package needs an entry in its rules table in the
same change, so an unlisted package fails the test instead of passing unchecked. Imports that only
test files may use are listed separately, so a fake never ships. A file that must break the clock,
goroutine or `/proc` rule is listed in `archcheck` with its reason, and an entry whose file no longer
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
| `--placement` | `VIBESYS_PLACEMENT` | `submit` only, ignored by the other commands: the one placement of the operator config a docker job may run on. |

Unset, `--placement` lets a docker job run on any placement that gives every resource its task
needs, and the daemon chooses one when the job starts (see "Placements are interchangeable"). Set,
it restricts the job to that one placement, and a submission is refused, naming the placement, when
the config has no such placement or it lacks a need. A submission no placement can run is refused,
naming what is missing. A host job has no placement: one inherited from `VIBESYS_PLACEMENT` is
dropped, and `--placement` given beside `--env host` is refused, naming the flag. Without `--env`,
every job runs where the task's check and measure run (see "Running in docker"), a command job too.

### Environment

These are all the environment names the CLI reads (through one `getenv`, which a test records).
The orchestrator sets every one of them for the agents it launches but `VIBESYS_PLACEMENT`. The
names are the `env_name` of the fields of `AgentContext` and `OperatorContext`
(`proto/vibesys/cli/v1/env.proto`), which `envcodec` reads and writes; a field that is empty is left
unset in the agent's environment, and a value a field's rules refuse is a usage error naming the
variable.
`VIBESYS_PLACEMENT` is the operator's: an agent inherits it from the environment it was started in.

| Name | Meaning |
| --- | --- |
| `VIBESYS_REPO`, `VIBESYS_TASK`, `VIBESYS_RUN`, `VIBESYS_HARNESS`, `VIBESYS_AGENT` | The defaults of the context flags above. |
| `VIBESYS_PLACEMENT` | The default of `--placement`, an optional restriction an operator sets at a run's start. |
| `XDG_RUNTIME_DIR` | Where the runtime directory is (see "The daemon and the supervisors"). Ignored unless it is an absolute path. |
| `VIBESYS_CLI_RUNTIME_DIR` | The runtime directory itself, in place of the one `XDG_RUNTIME_DIR` gives. The orchestrator sets it for its agents, so they reach the daemon it chose whatever else their environment says. A value that is not an absolute path is refused. |
| `VIBESYS_CLI_SANDBOX` | `1` makes the daemon a sandbox daemon, beside the one that holds the machine, in a runtime directory of its own, which `XDG_RUNTIME_DIR` or `VIBESYS_CLI_RUNTIME_DIR` must name (see "The daemon and the supervisors"). Any other value is refused. |
| `XDG_STATE_HOME` | Where the task index is: `$XDG_STATE_HOME/vibesys`, else `~/.local/state/vibesys`. Ignored unless it is an absolute path. Read once per runtime directory: the first start records the answer in `daemon-places.json` there, and later starts reuse it. |
| `HOME` | The home directory the index falls back to. |

The orchestrator also sets `VIBESYS_CLI`, the absolute path of this binary for the skills to run
(`"$VIBESYS_CLI"`); the CLI itself does not read it. Installing and upgrading
read their own settings (`VIBESYS_CLI_VERSION`, `VIBESYS_CLI_INSTALL_DIR`, `VIBESYS_CLI_RELEASES_URL`).

| Command | What it does |
| --- | --- |
| `submit check <commit> [flags]` | Queue the task's check. A pass already saved for the commit answers it without running. Also `--wait`, `--note`, `--no-cache`, `--plan` and `--env`. |
| `submit measure <commit> [--samples n] [--recheck] [--plan n \| --no-plan] [flags]` | Queue the task's benchmark for `n` samples (the task's default when omitted). Saved samples count: only the missing ones run. Flags as for `check`. `--plan n` says the measurement tests plan `n`, which the plan's view and a run's chart show beside the point; `--no-plan` says it tests none, as the baseline's does. A measure submitted from a run (a call that carries `--run`, or `VIBESYS_RUN`) must give one of them, since every point on a run's chart is a measure job and the plan is what the app shows beside it: without either the daemon refuses it as `INVALID_INPUT`, naming both flags. A person at a terminal, with no run, is not asked. The two are never given together, and `--plan 0` is not "none". Only a measure job takes `--no-plan`. An unknown plan is `NOT_FOUND`, naming `--plan`. |
| `submit command <commit> --image harness\|candidate [flags] -- <argv...>` | Queue a command and print its id. Returns at once; `--wait` blocks as `wait` does. `--image` is required, with no default: `harness` runs the command in the task's image, where its check and benchmark run (the host for a task with no Dockerfile), and on a task with `[candidate]` beside the candidate's server exactly as the check and the benchmark run (server started from `[candidate] serve`, readiness polled, `VIBESYS_SERVICE_URL` set, the same resource shares; the server's startup time is added to `--timeout`); `candidate` runs it in the image built from the commit's `[candidate] dockerfile`, with no server, and is refused for a task without `[candidate]` or with `--env host`. Also `--note`, `--no-cache`, `--plan`, `--env`, `--timeout`, `--service`, `--probe` and `--wrap`. A revision is resolved to a full commit id. `--service`, `--probe` and `--wrap` are split into words as a shell would split them, with single quotes, double quotes and backslashes, and nothing else of a shell. The daemon puts `--wrap` in front of the candidate's server for `--image harness` on a task with `[candidate]` (a profile: `--wrap '<profiler>' -- <load argv>`), and otherwise in front of `--service` when it is given and of the command when not; `--service` is refused beside the candidate's server. Never cached or scored; its artifacts are kept with it. `jobs` names the image of each command job, and the job's output starts by naming it for a docker job. (`submit profile` and the 0.2 `[profile]` it read are retired; a manifest with `[profile]` still loads.) |
| `wait <job>... [--any] [--timeout d]` | Block until the jobs are final, or with `--any` until one is. The daemon decides how the wait ended and keeps the timeout. |
| `cancel <job> [--reason text]` | Stop a job and print it once it is final. Cancelling a final job prints it unchanged. |
| `note <job> <text>` | Add a note to a job in any state. |
| `jobs [--commit rev] [--state s] [--note-contains text]` | List the task's jobs, oldest first, with why each queued job waits. |
| `logs <job> [--follow]` | Print a job's output, and with `--follow` keep printing until it ends. |
| `show <commit>` | Every result saved for a commit, oldest first: each check, and the commit's measurements pooled as `compare` pools them, one line per environment, task version and metric, naming every measure job it holds (a top-up adds its samples to the same measurement). A measurement's `value` is its headline number, the mean of its samples, computed by the daemon; each of its `observations` has the same `value` for its own samples. For a multi-objective task the line gives every objective's headline in the order the task declares them, the numbers `compare` weighs. With `--json`, `results` has each job's result and `measurements` the pooled ones. |
| `compare <commit> [--against rev]` | `better`, `worse`, `within_noise` or `inconclusive`, against the result branch head unless `--against` names a commit. For a multi-objective task, also `tradeoff`, with a verdict on every objective (see "Multi-objective tasks" below). Refuses to mix environments. |
| `promote <commit>` | Fast-forward the result branch to a commit that is `better` than its head, or print every reason it was refused. A refusal exits `1` and is printed on stdout. A promoted multi-objective commit is also offered to the frontier. |
| `result` | The head and a final number, its latest measurement that rests on a passing check of the same commit, task version and environment, or why there is none (exit `1`): `no_head` before the first promotion, and `head_not_measured`, which names the `submit measure <head> --no-plan` that gives the head one (see "The result branch"). |
| `frontier add <commit>` | Add a measured, check-passed commit of a multi-objective task to the task's frontier, evicting the members it beats, or print every reason it was refused. A refusal exits `1` and is printed on stdout; a commit of a single-metric task is `INVALID_INPUT`. |
| `frontier list` | The frontier's members in the order they were added, each with its headline number on every objective, computed by the daemon. |
| `plan create --title t [--body text \| --body-file path] [--gain-bound share] [--workstream n]` | Store a plan, open, and print its number. `--body -` and `--body-file -` read standard input. `--gain-bound` states the most the plan could gain, as a share (`0.05` or `5%`) of the headline; `--gain-bound-of` names what it is a share of (`headline` is the only value, and the default). `--workstream` puts the plan in a workstream of the task (`3` or `#3`), and a number the task has no workstream for is refused as not found, naming the flag. |
| `plan list [--status s] [--search text]` | List the task's plans by number. With no `--status` every plan is listed. A plan that states a gain bound prints it after the title (`gain <= 5% of headline`). |
| `plan view <n>` | Print a plan, its comments and the jobs linked to it (`submit --plan <n>` links one). |
| `plan edit <n> [--title t] [--body text \| --body-file path] [--status s] [--gain-bound share] [--workstream n]` | Change what is given and keep the rest. A gain bound replaces the old one; it is never cleared. `--workstream` moves the plan to another workstream; a plan's workstream is moved and never cleared. |
| `plan comment <n> --body text \| --body-file path` | Add a comment, which carries a decision or a result. |
| `plan close <n> --status s [--comment text \| --comment-file path]` | Set a status other than `open` and add a comment, as one change. |
| `plan check-gain --under share` | Whether every plan that is `open` or `parked` states a gain bound below the share (`0.1` or `10%`). It prints `yes: ...` or `no: ...` followed by one line for each plan that is not under (a plan that states no bound is not under), and exits `0` for either answer. With `--json` the answer is `answer` (`PLAN_GAIN_ANSWER_ALL_UNDER` or `PLAN_GAIN_ANSWER_NOT_ALL_UNDER`), `counted` and `blockers`. A bound equal to the share is not below it. |
| `workstream start <title>` | Start a workstream, a line of work such as "KV cache layout" that plans join, and print its number, status and title. A title that a workstream that is not closed has, ignoring case and runs of white space, finds that workstream and prints it, so planners that name the same line of work at once agree on one number. A title is one line of at most 80 characters, stored with its white space collapsed. With `--json`, a `StartWorkstreamResponse`, whose `created` says whether this call made it. |
| `workstream list [--status s]` | List the task's workstreams by number, one line each (number, status, title, and the note when there is one). `--status` is `active`, `paused` or `closed`; repeat it or separate with commas. |
| `workstream edit <n> [--title t] [--status s] [--note text]` | Change what is given and keep the rest. The status is the agent's to set and no order is enforced; the note is one line of state, such as `paused: accuracy gate`, and an empty `--note` clears it. A title that another workstream that is not closed has, ignoring case and white space, is refused as a conflict (exit `1`), and so is reopening a workstream whose title was taken since. An edit that sets what is already there changes nothing, the update time included. |
| `bound declare --objective name --value v --unit u (--note text \| --note-file path) [--commit rev]` | State the performance bound of an objective: the best it can reach, derived from first principles (its speed of light). The daemon reads the task at the commit (`HEAD` by default) and refuses an objective the task does not declare, naming it and listing the ones it does; it records which way the objective improves from the task, and who declared the bound and when from the call and its own clock. `v` is a number above 0 in the unit of the objective's own measurements, and the note says how it was derived. A declaration is never replaced: a revision is a new entry. It prints the entry's place in the log, the objective, the number with its unit, and the direction. |
| `bound list [--objective name]` | Every declared bound of the task, oldest first, one line each (place, objective, number and unit, direction, time, declaring agent and run, `current` or `superseded`) with the note under it. The current bound of an objective is its newest. With `--json`, a `ListBoundsResponse`. |
| `task card <task> [--revision rev \| --working-tree \| --folder dir]` | What a client shows of a task, read by the daemon from `vibesys.input.toml`, `objectives.toml` and the first heading of `OBJECTIVE.md`: the task, its heading, the domain, the hardware (a count of an accelerator from the closed registry of display names; an unknown name is refused naming the file and the key), the served model or the workload, and each objective with its direction and unit. By default it reads the task as committed at `HEAD` of `--repo`; `--revision` reads another commit, `--working-tree` the files as they stand under `--repo` (its root), `--folder` a folder that is not a repository. A task with no card exits 2 with the reason. With `--json`, a `TaskCard`. |
| `daemon ensure` | Make sure a compatible daemon answers, starting one if none runs (the start protocol below). It prints `ready`, the pid and the version, or with `--json` a `DaemonEnsureResponse`: the socket, the build, and the runtime directory. It exits `0` when a daemon answers and `4` when none does, whatever the reason, and the message says why; only a malformed command exits `2`. A program that starts the daemon, such as the orchestrator, runs this with `--json` and rebuilds no path. |
| `daemon status\|stop\|restart [--force]` | Report on, stop, or restart the daemon. The first line of `daemon status` counts all of the daemon's work: `queued`, `running`, and `submitting`, the submissions it has not answered yet (with `--json`, `submitting_jobs`). A check or measure submission is keyed before its job exists, which for a docker job can build its image and convert it for a cluster for hours, so a daemon with no queued or running job may still be busy. `daemon restart` leaves running jobs running under their supervisors, but a submission in progress starts over on the next daemon, so it refuses with `CONFLICT` (exit `1`) while the daemon has one, unless `--force`. `daemon status` also says what every job inherits from the daemon: the directory clean checkouts are made in (`checkouts`, from the daemon's `TMPDIR`), and one `env` line per environment variable, sorted, with its value only for `PATH`, `HOME`, `TMPDIR` and a `VIBESYS_` variable whose name has none of `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL` (with `--json`, `environment` and `checkout_directory`). Jobs see the environment the daemon was started with, so a variable set later reaches them only after `daemon restart` from a shell that has it. `daemon status` with no daemon prints `not running` (with `--json`, an `UNAVAILABLE` `Failure`) and exits `4`. `daemon install` is not available yet (exit `2`). |
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

A plan records who made it: `origin` is the run and agent the call carried (`VIBESYS_RUN` and
`VIBESYS_AGENT`, which the caller states and nothing authenticates) and the time the daemon took, and a
request has no field for it. `transitions` is the plan's status history, oldest first: its creation as
`open`, then one entry for each `plan edit` or `plan close` that set a status the plan did not have, with
the origin of that call. Setting the status a plan has adds nothing. A plan saved before store format 6
has no origin and no transitions, and none is invented for it: its transitions start at its first status
change after the upgrade. `plan view --json` and every fact of `WatchTask` carry both.

Workstreams belong to a task and are numbered 1, 2, ... by the daemon and never renumbered. A workstream is a
line of work an agent names, such as "KV cache layout", with a status (`active`, `paused` or `closed`), a
one-line note, and a `RecordOrigin` for who started it and when. Plans join one through `plan create
--workstream` and `plan edit --workstream`, and `Plan.workstream` says which. Nothing a workstream holds is
derived: how many plans it has and what they gained are read from the plans. No two workstreams that are not
closed have the same title, so `workstream start` is idempotent in the title, the daemon makes the choice under
the store's lock, and starts that race get one number. A closed workstream is named by no title, so its line of
work can be started again as a new workstream, and a closed workstream still takes plans. Every change of a
workstream is a fact of `WatchTask`, keyed `workstream/<number>`.

Bounds belong to a task and are an append-only log, numbered 1, 2, ... by the daemon. Declaring one is
the agent's choice, early or late, and the daemon only checks that it is well formed and about an
objective of the task: it never refuses a bound that a measurement has passed, since revising it is the
agent's to do. Each entry keeps its value, unit, note, direction, and a `RecordOrigin`: the run and agent of
the call and the time the daemon took. A `bound declare` or `plan create` makes a task's store, and `bound list`
prints nothing for a task with no store. A store made before bounds existed is migrated when it is
opened and has none declared.

Every command ends with one of five exit codes, and which one is a function of one answer: the
command's own yes or no, how a `wait` ended (a `WaitOutcomeKind`), or the kind of a `Failure`.

| Code | Meaning | Endings |
| --- | --- | --- |
| 0 | yes | success; `ALL_SUCCEEDED`, `FIRST_SUCCEEDED` |
| 1 | the answer is no | `SOME_FAILED`, `FIRST_FAILED`; a refused promote or frontier add; no current result; `CONFLICT`, `ENVIRONMENT_MISMATCH`, `NOT_MEASURED` |
| 2 | invalid input | usage errors; `INVALID_INPUT`, `NOT_FOUND` |
| 3 | timed out | `TIMED_OUT` |
| 4 | no answer | `UNAVAILABLE`, `INCOMPATIBLE`, `DAEMON_OTHER_BUILD`, `INTERNAL`, `UNSPECIFIED`; a kind or outcome this build does not know; a wait that could not learn how its jobs ended; no daemon for `daemon status` or `daemon ensure` |

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
`note`, `jobs`, `logs`, `show`, `compare`, `promote`, `result`, `daemon status` and the `plan`, `bound` and
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
on the sandbox's own. Its containers and Slurm jobs carry the scope of its own runtime directory, and
so do its leases: it takes them in `leases/` of its runtime directory, in a directory and under a
Lease name prefix named for that runtime directory on an ssh machine and in a cluster, and never in
the machine's directory of leases below, so a test's measure never waits for a real one. A
test that starts a daemon in-process holds no machine lock either.

A command that needs the daemon connects and, if none runs, starts one and waits for its readiness
signal. A daemon that fails to start closes the signal without it, so the client reports the failure
at once and does not wait out a timeout. The client and the daemon exchange their versions when they
connect, and a mismatch is a refusal that says which side is older and what to run: `daemon restart` for an old daemon, an upgrade for an old client.

A daemon of the same protocol but another build is never used silently. The handshake carries each
build's identity (`BuildVersions.binary_version`: the release version, or `dev`), compared for
equality and never parsed, so a release build and a local build are treated alike. When the
identities differ, the command line asks the daemon for its status. A daemon with no submission in
progress and no queued or running job is stopped and replaced by the command line's own build, with
no step for the user. A daemon with any of them is left alone, and the command ends with
`DAEMON_OTHER_BUILD` (exit 4), whose message names both builds, the daemon's pid and its work,
and says to wait for it to finish or to run `daemon restart`, which replaces the daemon while its
jobs keep running under their supervisors. `daemon status` and `daemon stop` address whichever daemon runs and do not apply this
rule. A daemon older than the protocol the command line needs is replaced as before, jobs or not,
since its jobs outlive it.

The runtime directory records where its daemon keeps state, in `daemon-places.json` (`DaemonPlaces`),
written before the first daemon starts there, and every later start reuses it, whatever its own
environment says; a daemon that starts with an environment saying otherwise logs both. To move the
store of a runtime directory, stop the daemon and remove the file. A command run with
`VIBESYS_CLI_RUNTIME_DIR` starts a daemon only if the directory records its places. If it does not, as
after a reboot, the command starts nothing and exits 4 saying so, because its environment is not the one
that chose that daemon; `daemon ensure` records them again, which is what the orchestrator does.

`daemon ensure` is that same start protocol as a command: it holds `start.lock`, so any number of
`ensure` commands and other commands that run at once start one daemon between them. A new client
replaces a daemon older than protocol 3 (`MinDaemonProtocolVersion`), because only a daemon of
protocol 3 can serve `WatchTask`. The entire ensure operation has a two-minute limit,
including the start-lock wait, handshakes, stop, child readiness and final status. Expiry
releases startup ownership and returns a typed unavailable failure with exit 4.

### Watching a task

`WatchTask` is a server-streaming call that tells a program everything it needs to show a task:
the head of the result branch, plans, workstreams (a fact keyed `workstream/<number>`), declared bounds (a fact keyed `bound/<place>`), promotions, jobs and their results, a judgment of each of the
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

- A docker job may run on every placement that gives each resource its task needs, or on the one
  `--placement` restricts it to, and the scheduler chooses among them when it starts it: the least
  loaded, by the fraction of its slots and of what its resources claim that is held, and of those
  as loaded, the one whose name sorts first. So two measures with two idle placements run at
  once, each alone on its own. The choice is the scheduler's `Placer`, behind the same safety
  rules as the policy: it can only pick among the job's options, and a job that may start on none
  waits. A job that started stays where it started, and a job no placement fits is refused.
- A job holds claims from its launch until its end is recorded. A docker job claims a slot of
  `placement:<name>`, the placement it runs on, which has the placement's `max_jobs` slots, and
  what the placement's resources claim: `kubernetes:<machine><kubeconfig>` for a cluster,
  `gpu:<machine>/<devices>` and `rocm:<machine>` for GPUs, which every placement that gives the
  same one shares, and `rocm:<placement>@<login node>` for the AMD GPUs of an `apptainer`
  placement, whose jobs each get the GPUs of a Slurm allocation of their own. A host
  job claims a slot of `placement:default` when that placement is local, and otherwise of `host`,
  one slot of its own. A measure job claims each of them whole, so a benchmark sample never runs
  beside another job of its placement or of another placement on its cluster or GPUs, while the
  jobs of other placements run. A command job chooses its placement as a check does.
- A cluster is held alone by every job that runs with it, of every kind: each job deploys into
  the cluster with CPU and memory the scheduler does not see, so two checks on one cluster could
  starve each other's pods. So jobs on a placement that gives a cluster run one at a time whatever
  its `max_jobs`, as do the jobs of every placement that gives the same cluster, and each takes the
  cluster's lease (below). `max_jobs` still runs jobs side by side on a placement without one.
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
  machine's lease. A Kubernetes cluster a placement gives is leased in the cluster (below), by
  every job that runs with it. GPUs
  take no lease of their own, since their machine's lease covers them, and a `slurm` or `apptainer` placement takes
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

- `submit command`: a command you give, run in a clean checkout of the commit, in the image it names
  (`--image harness` or `--image candidate`). It is never cached.
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

A manifest declares its task schema as `version = "0.1"` (one image) or `version = "0.2"` (two
images: `TaskManifestV02` in `proto/vibesys/cli/v1/task.proto`). A 0.2 task builds the candidate's
image from the commit's `[candidate]` Dockerfile, runs its server in that image with the placement's
GPUs, and runs each command in the task's image beside it, at `VIBESYS_SERVICE_URL`; the
candidate's image is recorded with each result and is not part of its key. See
[the task environment](../docs/contributing/cli-task-environment.md) for the keys and the decisions.

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
  no file of the candidate. A symbolic link of the directory is a link in the context, and a task
  is refused when it is read if a link is absolute, leaves the directory, points at a path the
  commit does not have, or is part of a cycle, since it would lead to nothing in the image; the
  error names the link (see [task environment](../docs/contributing/cli-task-environment.md)). The
  image is tagged (`vibesys-cli-task:<key>`) by a digest of those files and links and of the image ID, as the engine has it locally, of each image the Dockerfile builds on:
  each `FROM` and `COPY --from=` that is not a stage of the same Dockerfile or `scratch`, with the
  defaults of `ARG`s declared before the first `FROM` substituted. So it is built again exactly
  when a file of the task changes or when one of those images changes under its reference, such
  as a base rebuilt locally under the same tag; nothing needs removing by hand. A base the engine
  does not have yet is pulled by the build, and the next job builds once more on it, from the
  build cache. A reference that names a variable with no default is not looked up, and a change of
  its image is not seen. The candidate's image of a task of two images is named the same way, by
  the digest of its files and its Dockerfile's bases (`vibesys-candidate:<task>-<key>`). The first `submit` of a task builds the image before the
  job is queued, so that submission takes as long as the build. The image digest is recorded with
  each result and is what makes two docker results comparable, with the backend, so results from
  different images are never mixed.
- Commands run in a container as the invoking user, in a clean checkout of the exact commit. The
  checkout and the job's scratch directory are mounted at their own paths, so a path a command is
  given means the same inside. A task with `docker_in_docker = true` is refused: the container has
  no container runtime. A task for the `cuda` backend runs when its placement has a `gpu`
  resource, and a task for the `rocm` backend when it has a `rocm` resource; each is refused
  otherwise, with the resource it lacks named. Other accelerator backends are refused.
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
  | `rocm` | The machine's AMD GPUs: `--device /dev/kfd --device /dev/dri --group-add video` under docker, `--rocm` under Apptainer, and each of `ROCR_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES`, `GPU_DEVICE_ORDINAL` and `CUDA_VISIBLE_DEVICES` that Slurm set. Before the command, the container checks that its ROCm runtime sees a GPU; when it does not, the job fails before its command with why, naming the image's ROCm version and the machine's amdgpu driver version. In a task of two images the GPUs are the server's. |

  A task names the resources its jobs need in its manifest, `[environment] needs = ["kubernetes"]`
  (any of `gpu`, `kubernetes`, `mounts`, `rocm`). A job runs only on a placement that gives them all, and
  one restricted to a placement that lacks one is refused before anything is built, with
  `placement "<name>" has no resource "<kind>"`.
- A result's environment fingerprint records where it was taken, for telling where a number came
  from: the placement (`placement`), the machine, and what identifies each resource: the GPUs'
  model, count and driver (`amd_gpu_model`, `amd_gpu_count` and `amdgpu_driver` for AMD GPUs), the cluster's server and version (`kubernetes_server`,
  `kubernetes_version`), the mounts' names. `show` and `jobs` name the placement. None of it decides
  which results are compared or reused (see "Placements are interchangeable").
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

[placements.mi210]
kind = "apptainer"
[placements.mi210.remote]
host = "host-a"
remote_workspace_root = "/shared/vibesys"
[placements.mi210.slurm]
partition = "mi210"
[placements.mi210.apptainer]
submit_command = ["/opt/site/bin/sbatch-wrapper"]
submit_arguments = ["--account=acct-a"]
[placements.mi210.resources.rocm]
[placements.mi210.resources.mounts]
hf = "/shared/hf-cache"
```

| Key | Meaning |
| --- | --- |
| `[scheduler] policy` | Which job starts next: `fifo` (default) or `fair-share`. |
| `stop_grace_seconds` | How long a job's commands get to clean up after a cancel or the time limit asks them to terminate, before they are killed, on the host and on every placement (30 by default, at most 240). See "Stopping a command". |
| `[scheduler] max_jobs` | In a file of the earlier shape only: the `max_jobs` of its one placement. |
| `[placements.<name>] kind` | `local` (this machine's docker), `ssh` (docker runs on `remote.host`, reached over ssh), `slurm` (as `ssh`, with every container and image build inside a new allocation made by `srun`) or `apptainer` (a Slurm cluster reached through its login node `remote.host`, whose compute nodes run Apptainer and no Docker; see "The apptainer placement"). A name is lower-case letters, digits, `_` and `-`. |
| `[placements.<name>] max_jobs` | How many jobs may run on the placement at once (1 by default). A measure job runs alone on it, but on an `apptainer` placement of `[slurm] exclusive`, where it runs alone on a node of its own and takes one of the slots as any job does. More than 1 needs kind `local` or `apptainer`: an `ssh` or `slurm` placement stages and sweeps one job at a time on one shared Docker daemon, while each job of an `apptainer` placement has staged directories and allocations of its own (see below). |
| `[placements.<name>.remote] host`, `ssh_command`, `rsync_command` | For `ssh`, `slurm` and `apptainer`: the ssh destination (the login node, for `apptainer`) and the programs that reach it. |
| `[placements.<name>.remote] remote_workspace_root` | An absolute directory there for the copies of runs' directories (`staged/`) and the caches (`cache/`). For `apptainer` it must be on the file system the compute nodes share, and also holds the images (`images/`), the probe program (`bin/`) and the last reading of a node (`nodes/`). |
| `[placements.<name>.slurm] partition`, `gres`, `shared_gres`, `exclusive`, `cpus`, `memory`, `srun_arguments` | For `slurm`: what an allocation asks for, by what it runs: an image build asks for `cpus` (8 by default) and `memory` only, and builds in a BuildKit builder confined to them; a check asks for `shared_gres` (such as `shard:1` of a GPU Slurm shares; `gres` when unset) and the same share of the node; a benchmark sample asks for `gres` and, with `exclusive` (true by default), the whole node. Every container is confined to its allocation's CPUs. |
| `[placements.<name>.slurm] poll_interval_seconds`, `queue_timeout_seconds` | The pause between reads of a job's state while its cancel is confirmed, and the bound on the wait for an allocation (24 hours by default). |
| `[placements.<name>.slurm] max_time_seconds` | For `apptainer`, optional: the longest time limit the cluster grants a job of the placement, as a site's QOS or partition caps it, a multiple of 60 (Slurm counts minutes). No allocation asks for more: a reused allocation is `allocation_limit_seconds` long, at most this (this by default); a container's own allocation is its run's limit and 10 minutes; and a run whose limit and those 10 minutes do not fit is refused before anything is submitted, with an error that names this key and both numbers, instead of the site's `sbatch` error. Unset, there is no cap. See "Reused allocations". |
| `[placements.<name>.slurm]` for `apptainer` | The same keys but `srun_arguments`, which is refused: a run's allocation is a batch job, which asks for `gres` and, with `exclusive`, the whole node when it measures, and `shared_gres` (or `gres`), `cpus` and `memory` when it does not. |
| `[placements.<name>.apptainer] submit_command`, `submit_arguments`, `program` | For `apptainer`, all optional: the command that submits a batch job on the login node (`sbatch`, or a site's wrapper that takes its arguments), the options passed to it after the ones the CLI sets (such as an account or a QOS the site requires on every job), and the apptainer program of the compute nodes (`apptainer`), which only runs SIFs. |
| `[placements.<name>.apptainer] local_scratch_dir`, `convert_timeout_seconds` | For `apptainer`, optional: the absolute directory of this machine where an image is converted to a SIF (the archive, the image unpacked and the SIF, which is kept there until the cluster has it; `vibesys-cli/apptainer` under `XDG_CACHE_HOME` or `~/.cache` by default), and the bound on converting one image and copying its SIF, or on streaming its files and making its squashfs under `pyxis` (3600 by default). See "The apptainer placement". |
| `[placements.<name>.apptainer] idle_release_seconds`, `allocation_limit_seconds` | For `apptainer`, optional: reuse allocations. With `idle_release_seconds` set, a container's allocation is kept when the container is removed, for the next container of the daemon's scope (of the same job or a later one) to claim, and it ends itself once no container has claimed it for that long; each such allocation asks for the whole node (`gres`, and `exclusive`) whatever it runs, and its time limit is `allocation_limit_seconds` (`[slurm] max_time_seconds` by default, or a day without it; at most the partition's, and at most `max_time_seconds`). Unset, each container has an allocation of its own, as before. See "Reused allocations". |
| `[placements.<name>.apptainer] runtime` | For `apptainer`, optional: what runs the containers on the compute nodes, `apptainer` (the default) or `pyxis` (Slurm's Pyxis plugin over Enroot, for a cluster without Apptainer). `pyxis` refuses `program` and `local_scratch_dir`, and needs no Apptainer on this machine. See "The pyxis runtime". |
| `[placements.<name>.allocations] max_held`, `max_for` | Optional: lets a run hold standing allocations of the placement, at most `max_held` at once (1 to 64, pending and running, renewals included), each asked for at most `max_for`, a duration such as `"24h"` (for `apptainer`, at most `[slurm] max_time_seconds`). Unset, the placement offers none. See "Standing allocations". |
| `[placements.<name>.allocations] fit_margin`, `run_idle_end` | Optional: the time a job's fit keeps free before its allocation ends (`"2m"` by default), and how long a run holding allocations of the placement may make no call before it is taken to have ended, as `alloc end-run` ends it (`"2h"` by default, at least `"1m"`). |
| `[placements.<name>.resources.gpu] devices`, `runtime` | A `docker --gpus` value such as `all`; with `runtime` (such as `nvidia`) the GPUs are passed by that runtime and `NVIDIA_VISIBLE_DEVICES` instead of `--gpus`. |
| `[placements.<name>.resources.kubernetes] kubeconfig`, `registry`, `builder`, `network` | An absolute path of the kubeconfig on the engine's machine (required), the registry and image builder addresses the container gets, and its network mode (empty leaves docker's default). |
| `[placements.<name>.resources.mounts] <name> = "<dir>"` | A directory of the engine's machine every container sees read-only at `/vibesys/mounts/<name>`; for `apptainer`, a directory of the compute nodes, such as one on the shared file system. |
| `[placements.<name>.resources.rocm]` | An empty table: the machine's AMD GPUs, for tasks of the `rocm` backend. An `apptainer` placement gives GPUs only this way; it refuses `gpu` and `kubernetes`. |

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

### The apptainer placement

An `apptainer` placement runs the same docker environment on a multi-node Slurm cluster reached
through its login node, whose compute nodes have Slurm and Apptainer and no Docker. This machine
needs Apptainer too, on the daemon's `PATH`: a placement of the kind is refused at the daemon's
start without it, saying to install it.

- The images are built by this machine's Docker, as for every placement, and converted to SIFs here,
  where they already are, by this machine's `apptainer build`: neither the nodes' scratch space nor
  the shared file system limits the conversion, and only the SIF crosses the network. Before a
  container of an image starts (and as part of a build, whose log then says which apptainer, of
  which version, converted it), unless the cluster has the image's SIF, `docker save` streams the
  image into a directory of its own under `local_scratch_dir`, which also holds Apptainer's
  temporary files (`APPTAINER_TMPDIR`, `APPTAINER_CACHEDIR`) and is removed when the conversion
  ends; the SIF is renamed from it to `sif/<digest>.sif` under `local_scratch_dir`, where it is
  kept until the cluster has it. A run that finds a SIF kept there copies it and converts nothing.
  The directory's file system is checked first for the image unpacked (as `docker image ls`
  reports it) and its archive and SIF (each about the image's stored layers); when it has too
  little, the run fails before its command naming the space needed, the directory, and the space
  there is. A 47 GB ROCm image converts in about ten minutes to an 11 GB SIF.
- A SIF, the SIF kept here, and the lock of its conversion are named by the digest of the image's
  content, `<digest>` below: a SHA-256 over what `docker image inspect` says a container of it
  runs, its `Config` (keys sorted), its `Os`, `Architecture` and `Variant`, and its `RootFS` layers'
  diff IDs, in order. They are not named by the image's ID: under Docker's containerd image store
  the ID is the digest of the image's manifest or index, and BuildKit's attestations make it
  differ from one build to the next of the same Dockerfile and files, so a commit that changes only
  files the Dockerfile never copies would otherwise convert and copy the whole image again. Builds
  of one content share one SIF. SIFs that earlier versions named by image ID stay on the cluster,
  used by nothing; remove `images/` files older than the upgrade to reclaim their space.
- The jobs of this machine convert one image one at a time, whichever process runs each: a
  conversion holds a lock on the file `vibesys-convert-<digest>.lock` in `local_scratch_dir`
  (`flock`, which the machine lets go of when the process ends, however it ends). A job that finds
  it held says in its log that another process of this machine is converting the image, tries
  again every `[slurm] poll_interval_seconds` (5 by default) for up to `convert_timeout_seconds`,
  and then uses the SIF the other copied, or carries on where the other stopped: it copies the SIF
  the other kept, or converts the image itself. The lock files stay, one per image converted.
  Conversions of different images still run at once, and the check of the free space does not count
  what the others are about to write, so two that start together can each find room for itself and
  not for both.
- Once the conversion has ended, the SIF is copied over ssh to `images/<digest>.sif` under
  `remote_workspace_root`, with `images/<digest>.sif.built-by` beside it naming what converted it:
  no connection waits on the conversion. The copy is written to a file of its own and renamed into
  place once its size is what was sent, so daemons of different machines that copy one image at
  once each publish a whole file, and a copy cut short, or a conversion that fails, leaves none.
  A copy whose ssh connection fails (ssh's exit status 255) is made again after `[slurm]
  poll_interval_seconds`, up to 3 copies in all, each said in the job's log; a copy that fails
  otherwise, or the third time, fails the run and leaves the SIF kept here for the next run of the
  image. Once the cluster has the SIF, the one kept here is removed; one kept for more than a day
  (its copies failed, and no run of its image came since) is removed by the next conversion. Every
  later run, of any daemon, finds the SIF on the cluster. `convert_timeout_seconds` bounds the
  conversion and the copy together.
- A run's first container is a batch allocation of its own (`sbatch --parsable --no-requeue`, or the
  site's `submit_command`, with the `[slurm]` options and `submit_arguments`), named by the
  container and commented with the daemon's scope. Its time limit is the command's limit and 10
  minutes for the stop. A holder step keeps it for the run: when the run ends, or the daemon dies
  and its ssh connection with it, the holder cancels the allocation.
- Unless allocations are reused (below), a run is one command of a job, so each command has an allocation of its own: a check job's
  stages, and a measure's check and each of its benchmark samples, each wait in Slurm's queue
  again. On a busy partition a measure can wait between its samples. While an allocation waits, the
  run says why in the job's log, before its command's output: Slurm's state of the allocation, its
  reason and its estimate of the start (`squeue -o %T|%r|%S`) when the wait begins and each time
  one of them changes, and the node and how long it waited once it runs. A run whose allocation
  runs at once writes nothing of it. `vibesys-cli jobs` shows such a job as running.
- The AMD GPUs a job gets are its allocation's: Slurm keeps two allocations off one GPU, and gives
  a measure the whole node with `[slurm] exclusive` (the default). So the GPUs are claimed per
  placement, `rocm:<placement>@<login node>`, and a measure is alone on its node without holding
  its placement alone: it takes one of the placement's `max_jobs` slots as any job does, and other
  measures and jobs of any task run beside it, each in allocations of its own, on other nodes. A
  reused allocation (`idle_release_seconds`) is sized for a measure and runs one job at a time, so
  it is a node of its own too. With `exclusive = false`, a measure's allocation is a slice of a
  node whose other slices may run other jobs, of this daemon's other placements or of anyone, so a
  measure holds its own placement alone, and the jobs of the cluster's other placements run beside
  it. Nothing of a placement of this kind
  is leased between daemons: Slurm, which grants each its own allocations, is what keeps them
  apart.
- The machine's facts and load, and the AMD GPUs' facts, are read on the node inside the
  allocation, never on the login node. A daemon fingerprints a placement before it has a node, so
  its fingerprint names the last node a run of its scope read (kept at `nodes/<scope>.json`), and
  says `unread` before any.
- Each of a container's processes is a step of the allocation (`srun --jobid --overlap`) that runs
  `apptainer exec` of the SIF with a clean environment, the container's variables, its mounts
  (read-only ones with `:ro`), the run's directories (staged under `remote_workspace_root` as for
  `ssh`), a private home at `/tmp`, `--rocm` with a `rocm` resource, and none of the site's bind
  paths (`--no-mount bind-paths`), which can bind the whole shared file system read-write and would
  let a container write the SIFs and other runs' directories; the node's `/etc/hosts` is bound back
  read-only, so `localhost` resolves. The image's own files are read-only, unlike in a docker
  container. SIGTERM and SIGKILL go to every process of the container,
  found by a variable each carries; the grace and the kill are as under docker.
- In a task of two images, the command's container joins the server's allocation, so both run on
  one node and share its network. The server's port is one the node has free, chosen in the
  allocation, and the command's container probes it with this binary, which is copied to the
  cluster (`bin/`) once and must be a static Linux build, as a release is.
- Removing a run's first container cancels its allocation and confirms it ended, with `squeue` and
  then `sacct`; a run first cancels every allocation of its scope a dead run left. An exit code that
  is ssh's own (255), or a step that never started its command, is never taken for the command's:
  the command's ending is then unknown.
- A login node that briefly refuses new ssh connections, as sshd does under a burst of them
  (`MaxStartups`: ssh exits 255 saying `kex_exchange_identification: Connection closed by remote
  host`), fails no job. A call over ssh that never reached the login node (ssh exited 255 and said
  it could not connect: the key exchange closed or reset, the connection refused, or not set up
  within ssh's `ConnectTimeout` of 30 seconds) never ran, so it is made again, waiting 1, 2, 4, 8
  and 16 seconds and then 30 seconds between attempts, 14 attempts over about four and a half
  minutes: the polls of a queued allocation, its submission and cancellation, the steps that hold
  or claim it and run its commands, the reads and signals of its node, and the rsync copies of the
  run's directories. A call that reached the login node is never made again, whatever it ended
  with: a program that failed, or a connection lost once its session was open (the command may
  have run). Each attempt of a call that should answer at once is bounded by its own two minutes
  (`<login node> did not answer within 2m0s`), so the waits between attempts never use the bound
  up.
- A read of a queued allocation's state that fails as unavailable (the login node, or Slurm, did not
  answer) says nothing of the allocation, which keeps its place in the queue: the run says so in
  the job's log once, and goes on reading every `[slurm] poll_interval_seconds`. Only reads that
  keep failing for 15 minutes fail the run, and its allocation is then cancelled, as on every path
  where the run ends before its command.
- The step that holds a running allocation is one ssh connection that lasts the run. A login node
  that refuses new connections leaves an open one alone, and a connection that is lost (no answer
  for about three minutes, `ServerAliveInterval`) cancels the allocation: that is how a daemon that
  died leaves no allocation behind, so a lost holder is not reconnected. The step is started again
  only while it never reached the login node.
- Several jobs of one placement run at once, up to its `max_jobs`, each in allocations of its own,
  which Slurm keeps apart (with `exclusive`, the default, a measured container holds its whole
  node). Each job's runs are a scope of their own under the placement's: its allocations are
  commented `<placement scope>.<run scope>`, and its checkouts are staged in
  `staged/<placement scope>/<run scope>/` under `remote_workspace_root`. A job's process holds a
  lock of its run scope (in `placement-runs/` of the daemon's runtime directory) for as long as it
  lives, so a run tells a dead job from a live one: before it starts, it cancels the allocations and
  removes the staged directories its own job's earlier attempts and every dead job of the placement
  left (an earlier binary's among them), and leaves a live job's alone. A measure job still runs
  alone on its placement: besides the node, it shares this machine (image builds and conversions)
  and the login node with whatever runs beside it, and `exclusive` may be off.

### Standing allocations

A run can hold capacity over time instead of asking for it per job. Opt-in per placement: with no
`allocations` section, `alloc request` is refused with an error naming
`placements.<name>.allocations`, and a job submitted without `--alloc` runs exactly as it always
has, whatever allocations exist. The agent guidance is the `allocations` skill (`skills/search/allocations`).
With an `[placements.<name>.allocations]`
section (`max_held`, `max_for`, `fit_margin`) a placement of kind `local` or `ssh` takes standing
allocations as slots of its own `max_jobs`; they have no end. A placement of kind `apptainer` makes them
batch jobs of its cluster (`apptainer.NewAllocator`, owned by this daemon's persisted identity), sized as a
measured container's allocation, and a job submitted into one runs in it as `srun --jobid <id> --overlap`.
A placement of kind `slurm` has no allocator, so this build refuses an allocations section on it, naming
the placement. To turn them on for an apptainer placement, add to it:

```toml
[placements.cluster-a.allocations]
max_held = 2        # at most two allocations at once, pending and running
max_for = "24h"     # each asked for at most this; at most [slurm] max_time_seconds
fit_margin = "2m"   # a job fits only when this much time is left after its bound
run_idle_end = "2h" # a run that makes no call this long is taken to have ended
```

A run is alive while it calls: every command that carries its `--run` (or `VIBESYS_RUN`) counts as
a sighting, kept to the minute. A run that holds allocations and makes no call for the
placement's `run_idle_end` is taken to have ended, as if it had run `alloc end-run`: its
allocations end (`REQUESTED`) and a renewal order `--until run-end` asks for no more. So an agent
that dies without ending its run holds its allocations for at most `run_idle_end` after its last
call, and the backend's time limit bounds them in any case. A daemon start counts as a sighting
of every run, since no run can be heard while the daemon is down.

Every command below needs a run (`--run` or `VIBESYS_RUN`) and `--json` gives the typed message, except
`placement capacity`, which names no run unless it is to list the run's held allocations.

| Command | What it does |
| --- | --- |
| `alloc request <placement> --for <dur>` | Record a request and return, usually with the allocation `PENDING`. |
| `alloc ls [--ended] [--all-runs]` | The run's allocations that have not ended; `--ended` adds the ended ones, `--all-runs` lists every run's. |
| `alloc show <id>` | One allocation: placement, phase, start and end, renewal order, jobs. |
| `alloc wait <id> --until running\|ended --timeout <dur>`, `alloc wait <id> --until ending-in <dur>` | Return when the allocation is in the state; bounded by `--timeout`. Exit `0` met, `1` ended first, `3` timed out. |
| `alloc end <id> [--after-jobs]` | End now, or once its jobs are done. |
| `alloc renew <id> --lead <dur> --for <dur> [--until <RFC3339>\|run-end]`, `alloc renew <id> --off` | Ask for a successor once the time left is at most `--lead`; `--off` takes the order away. `run-end` stops at `alloc end-run`, or once the run has made no call for `run_idle_end`. |
| `alloc end-run` | End every allocation of the run and ask for no more. |
| `placement capacity <placement> [--run <id>]` | What the daemon measured of a placement: slots free and in all, for a cluster its nodes, waiting jobs and the reservations the account may use, the run's held allocations, and how long recent allocations waited. Facts only, no prediction. For a cluster, a node Slurm counts idle that it shows as reserved is free only when a reservation under way that holds it is one the account may use. |
| `submit ... --alloc <id>\|any` | Run the job inside that allocation. A named one that cannot fit the job's bound is refused (`CONFLICT`) with both durations; `any` falls back to the placement's own capacity when nothing fits, and the job's notes and the head of its output say why (`allocation any: ...; queued without one`). A job's bound is what it is held to: a command's own timeout (with the startup time of the candidate's server it runs beside, if any), and for a check or a measure the limits of its task's commands and servers added up, so a task of minutes fits an allocation of minutes. The bound counts from when the job's first container enters the allocation, after its image is built and converted: a job ready later than the allocation's end less its bound and `fit_margin` does not enter, and a named one fails (`CONFLICT`) saying how late it was and its limit, while one of `any` runs without the allocation and its output says why. With no flag a job runs as before. |

A refusal is a failure of the existing kinds: a request that cannot be right is `INVALID_INPUT`, one the
state refuses is `CONFLICT`, an allocation nobody holds is `NOT_FOUND`. The daemon keeps its allocations
in a log and rebuilds them on restart, adopting what the backend still holds and releasing what no record
names. The log and the daemon's identity are its own, under the state home's `vibesys/daemons/<key>`,
keyed by the machine and the runtime directory as its run records are, so daemons that share a home
never share them. The log records the limits each start ran under, and replays every decision under
them, so changing `max_held`, `max_for` or `fit_margin` changes nothing already decided. A daemon
started without an allocations section that a placement had ends that placement's allocations
(`EXTERNAL`) and cancels what its cluster still holds of them. When a job's container leaves a
standing allocation and its processes there cannot be confirmed killed, the job's supervisor says so
in the job's outcome, and the daemon ends that allocation (`EXTERNAL`) and cancels its batch job before
the next job could start in it: the jobs queued in it are given up as when it ends otherwise, so no job
runs beside the processes. Only changes of what an allocation is
are logged: why a pending one waits and when Slurm expects it to start are shown by `alloc show` and
kept in memory. This needs protocol 6.

### Reused allocations

On a busy cluster each allocation can wait in the queue for hours, and a measure of five samples asks
for six. With `idle_release_seconds`, an `apptainer` placement reuses its allocations instead:

- A run's first container claims a running allocation of the daemon's scope that has the run's
  limit and 10 minutes left, the one with the least time left first, and asks for a new one only
  when none can be claimed. A new one is named `vibesys-apptainer-pool-<container>`, asks for the
  whole node as a benchmark sample does, and has `allocation_limit_seconds` (or the run's limit and
  10 minutes, if longer). The job's log says when an allocation waits in the queue, as before.
  Once the new one runs, another container of the placement may find it in the listing and claim it
  before the container that asked for it does. It is then that container's, not cancelled, and the
  one that asked for it looks again, claiming a running one or asking for another, named
  `vibesys-apptainer-pool-<container>+2` (`+3`, ...), so that cancelling it never ends the first.
- No allocation asks for more than `[slurm] max_time_seconds`, which a site that caps a job's time
  (a QOS that refuses anything over 12 hours, say) sets to that cap: `allocation_limit_seconds`
  defaults to it and may not exceed it, and a run whose limit and 10 minutes do not fit under it
  is refused before anything is submitted. Every run of a job has a limit (its command's
  `timeout_seconds`, the server of a task of two images included, which runs under the limit of
  the command beside it); a container given none claims the allocation with the most time left,
  or asks for `allocation_limit_seconds`, and runs as long as its allocation lasts.
- A claim is a step on the allocation's node that holds a lock file there (`flock`, in
  `/tmp/vibesys-pool-<job>`). One claim at a time: no other container runs in the allocation
  beside a measured one, except the one that joined it in a task of two images. Before a claim is
  held, every process of a container left in the allocation is killed, and the containers' homes
  are removed.
- Removing the container releases its claim: its processes are killed, the release waits until
  none is left, and the allocation waits for the next claim. A release that is not confirmed
  cancels the allocation instead.
- The allocation's own batch script looks at its claim every 30 seconds: it clears what a dead
  claim left (a daemon that died, whose claim step died with its ssh connection), and once no claim
  has lived for `idle_release_seconds`, it marks the allocation ending, so nothing claims it, and
  exits, which ends the allocation. So a daemon that stops, or dies, leaves an allocation for at
  most `idle_release_seconds` and 30 seconds, and a later daemon of the same scope claims it in the
  meantime.
- A run's sweep of the leftovers of its scope leaves the reused allocations, which are not
  containers; a claim clears what they ran. The reused allocations are the placement's, claimed by
  any of its jobs, one claim at a time, so a daemon's jobs that run at once never wait on each
  other's claims: a job that finds none free asks for a new allocation.

### The pyxis runtime

An `apptainer` placement with `runtime = "pyxis"` runs its containers with Slurm's Pyxis plugin
over Enroot instead of Apptainer, for a cluster whose nodes have no Apptainer. Everything above
holds but how an image reaches the cluster and how a process runs in it:

- Nothing is converted on this machine, which needs no Apptainer. Unless the cluster has it, an
  image's files are streamed over ssh, as a container of it has them (`docker export`), to the login
  node, whose `sqfstar` (squashfs-tools 4.6 or later) makes them `images/<digest>.sqsh` under
  `remote_workspace_root`, with `.built-by` beside it. Neither side writes the image unpacked to a
  disk. The image's environment is written to its `/etc/environment`, which Enroot reads; Docker's
  own files (`/.dockerenv`, the devices under `/dev`) are left out, as Enroot's own import leaves
  them out. The stream ends with a file naming the image's content (`/.vibesys-image`), and the
  squashfs is renamed into place only once it has that file, so one cut short is never there. Makers
  of one image on one login node take turns (`flock`), and one that finds the squashfs made keeps it.
  `convert_timeout_seconds` bounds the stream and the squashfs.
- Each of a container's processes is a step of the allocation, `srun --jobid --overlap
  --container-image=<squashfs>`, read-only (`--container-readonly`) and unnamed, so nothing of it
  outlives the step, with the container's mounts, its directory (`--container-workdir`), its home
  at `/tmp` (a directory of the node's, made by a step before, which every process of the container
  shares), and none of the user's home, the image's entrypoint or a root remapping. The step is
  started with an empty environment but `PATH` and the container's variables, which win over the
  image's (`--container-env`), so nothing of the login node's environment reaches the container;
  Slurm's own variables of the step, and the site's, do. The
  site's Enroot binds the node's `/dev`, with its GPUs, and its own files (`/etc/hosts` among them)
  into every container, and Pyxis passes in the variables in which Slurm names the GPUs it assigned.

### Placements are interchangeable

Where a sample ran does not decide which results are compared, pooled or reused. Comparison,
promotion, the frontier, check-pass and sample reuse and the cache key all go by what defines the
measurement: the commit, the task version, the schema version, and the environment's kind, backend
and image digest. The placement, the machine and the resources' facts are recorded with each result
and take no part. A difference between placements is noise that the noise floor and repeated
samples absorb; a task whose number depends on the hardware pins it through its needs, or an
operator through `VIBESYS_PLACEMENT`.

A job is keyed when it is submitted with the image digest of the first placement it may run on, by
name. When the engine of the placement it starts on built the image to another digest, the job runs
and saves its result under the key of that digest. A job whose task changed while it waited fails
instead, since its submitter asked for a result of the task as it was.

A store of a build before this one keyed docker results by their placement and machine as well, so
the same commit and image measured on two placements had two keys. Such a result is keyed again
when it is read, as is the job that saved it, so the results of every placement pool and are reused.
A host environment's facts still identify it: its results are compared only on a host with the same
operating system, architecture, CPU model and CPU count.

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
commands use, and `result` reports it. `result` gives the head the latest measurement of that exact
commit that rests on a passing check of the same commit, task version and environment, whenever it
was taken and whatever moved the branch there: the measurement a promotion rests on counts, and so
does one of a commit made on the branch, such as a task switch. Measurements of other commits never
count, so nothing taken under another task version stands for the head (a commit's task version is
that of the task its tree holds), and a failed check or measure saves no result. A head with no such
measurement is `head_not_measured`, and `submit measure <head> --no-plan` gives it one.

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

The frontier is the set of the task's commits that no member beats, under the task's current
version: that of the task at the result branch head. Only measurements under that version count.
`frontier add` judges the commit against each member with the comparison `compare` makes: it evicts
a member it is `better` than, keeps one it is a `tradeoff` against, retires a member measured only
under other task versions (a task switch starts the frontier afresh, and nothing is compared across
versions), and is refused with every reason that applies: `unmeasured` (the commit or a member), `check_missing`, `stale`
(the commit was measured only under another task version), and, each naming the `member` it is
about, `dominated` (the member is better), `within_noise`, `inconclusive` and
`environment_mismatch` (measured in another environment under the current version). A commit that is
a member already is answered `already_member` and nothing changes. The JSON answer is
`decision.admitted` (`added`, `evicted`, `retired`, `already_member`, `members`) or
`decision.refused.reasons`. A successful `promote` of a multi-objective commit offers it to the
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
runs the tests twice. `./check.sh race` and `./check.sh plain` run one half each (formatting and the first run,
and the vets and the second run), which is how CI runs them, as two jobs at once. The first run is every
package under the race detector, which covers the packages a later change adds without listing them. The
second is every test again without the detector, as the binary ships, with a wider range of simulation
seeds; the tests that drive a real Docker daemon run beside it, where a daemon is reachable. The tests include the architecture rules above,
building a real release with `build_release.sh` and installing it with `install.sh`, so they need
`go`, `curl`, `awk` and `sha256sum` or `shasum`. CI runs the same script as the `cli` and
`cli-plain` jobs (`cli_race_checks` and `cli_plain_checks`, each with a 600 s timeout and a 7 minute job limit).

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
- `internal/sim/alloc_test.go` runs runs holding standing allocations of a cluster placement (a fake
  backend) and of a machine's slots through faults, outages and restarts, and fails unless the default
  seeds reach each thing the invariants are about, each in a stated fraction of them: a job running in
  an allocation, a time limit, a cancel from outside, a run's end, a renewal and a restart.
  `internal/sim/standing_property_test.go` runs the same life over the daemon's production wiring
  (`production.Process.Wire`), the apptainer allocator and the cluster fake, and
  `standing_e2e_test.go` follows one allocation from the operator config to a job that runs in it and
  a renewal.
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

The tests can run from a shell that has a live daemon's environment. The tests of
`cmd/vibesys-cli` start the tool, its daemon and its supervisors, and their `TestMain` removes
every `VIBESYS_` and `XDG_` variable before they run. So a test reaches no live daemon's socket,
index or operator config; each test sets only the names it needs. A test runs the test binary with
these names set to directories of its own, and checks that it passes and writes nothing there.

The module pins Go 1.25.9 with a `toolchain` line: under `testing/synctest`, Go 1.25.0 corrupts its
own runtime state at random, which the simulator hit as fatal errors and stalls.

The tests do not depend on time, threads or the network: the clock is injected, the crash tests fail
a step by its number on an in-memory filesystem so the same step fails on every run, and a wait in
a test is bounded only to guard against a hang. A real directory is used only to run the contract
suite once on the operating system's filesystem.
