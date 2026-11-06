# Process boundaries

Managed runs span several programs: clients (the terminal client and the
desktop app), the host that owns a run's state, one session holder (the
sidecar) per agent, the job tool `vibesys-cli` with its daemon and supervisors,
and the agent itself. This page is the rule for what crosses between them, the
design that follows from it, the boundaries that exist and who owns each, and
what is not built yet. When code and this page disagree, fix whichever is wrong
in the same PR.

## Design rules

These hold for every link between two of our programs. The sections below apply
them.

Carriers, in order of preference:

1. Two programs that live a long time together talk over a socket with typed
   RPCs.
2. A process starts from one typed message, not from flags, files and the
   caller's environment.
3. A durable fact is a proto record.
4. An environment variable exists only as the output of a codec over a message.
5. Anything else is an external protocol, confined to one adapter.

Every link behaves the same way:

- It opens with a handshake that negotiates compatibility.
- Every call has a deadline.
- A command carries an id, so repeating it is harmless.
- A read returns a snapshot together with a sequence number, so a watcher can
  continue from it.
- An outcome is a typed value; free text is only for display.
- Liveness comes from a lock or from the link itself, never from a pid or from
  text.

Ownership and shape:

- The callee owns the contract, and the program that owns a fact serves it. A
  caller does not read the owner's files.
- A field says what the peer needs. The test is whether swapping the
  implementation would change the message.
- A stored record, an internal-link message and a client message are separate
  messages, composed rather than copied, so a client's type cannot name a
  private fact.
- Prefer deleting a boundary to typing it.

## The rule

Anything that crosses a process boundary is a typed `.proto` message: one
definition, validated with protovalidate, with generated code on each side. A
file, a flag, an environment variable, a socket path or a line of text that two
of our programs must agree on is a boundary too, and gets a message.

The message must not leak how something is done. Each field is what the peer
needs. The test is a swap: if replacing the sidecar with a Go program, Docker
with Apptainer, one agent harness with another, or SQLite with another store
would change the message, the message names the mechanism and is redesigned.
Typed messages make these leaks easy to see in review, which is the point.

Three consequences:

- A peer that must read a pid, a path or an environment name is named in the
  field's comment, together with why it needs it. A reader whose job is to
  signal a process (the host reaping a dead session holder) may carry a pid; a
  client never does.
- Display text (a log tail in an error) is never the reason for a decision. The
  reason is a typed value; the text is added for people.
- An external protocol we do not own (MCP framing, the Docker CLI, `git`, `ssh`,
  Kubernetes JSON, `nvidia-smi`, the agent harness's plugin manifest) is parsed
  in one package that converts to typed data at once, with a Fake or recorded
  fixtures. That package is listed in the inventory baseline with the reason
  `external:<protocol>`.

Two kinds of boundary are exempt on purpose. The installer and the release
index are read by POSIX `sh` before any binary of ours exists, so they stay one
token each: the latest-version file, the checksum lines, the asset names and the
installer's own settings. A task author's minimal result file (one JSON object
with one number) is a deliberate public contract for people who do not use our
SDK.

## Enforcement

Each check names the path, the rule and the fix when it fails.

| Check | Holds | Where |
| --- | --- | --- |
| Boundary inventory | Per file, the exact count of file reads, file writes, environment reads, `encoding/json` uses, command starts and `os.Args` uses in the host and orchestrator Go code. A new use fails until the change adds it, with a reason, to the baseline; a removed use fails until the baseline shrinks. | `orchestrator_checks` and `gateway_checks` (the `archcheck` packages of each module) |
| Environment names | Every `VIBESYS_*` and `XDG_*` string in the managed-run product code is in a shrink-only allowlist until a message carries it. | `scripts/check_boundaries.py`, the always-on `doc-checks` job |
| Mechanism words | Message, field, enum and rpc names in the orchestrator, `cli`, desktop and evaluator packages that contain a mechanism word (path, dir, file, pid, process, socket, json, sidecar, provider, worktree, marketplace, home, xdg, docker, sqlite, claude, codex) are in a shrink-only allowlist. | `scripts/check_boundaries.py` |
| Unvalidated messages | Every `cli.v1` message that has fields and no protovalidate rule is in a shrink-only allowlist. Empty messages are exempt. Each daemon and supervisor server validates the request of every RPC at its edge (`wire.RequestValidator`), and the store and run records validate on read, so a bad value is refused naming its field. | `scripts/check_boundaries.py` |
| Release gate | A `vibesys-v*` tag fails while `orchestrator/v1` is outside the breaking check. | `managed-release.yml` runs `scripts/check_boundaries.py --release` |

Limits, stated so nobody reads more into the checks than they do:

- The `cli` module has no inventory yet. Its contract is already callee-owned and
  most of its external parsers are confined by its own `archcheck`; the
  environment-name check covers its names.
- The mechanism-word check reads the `.proto` text, not the compiled descriptors,
  and has no per-name reason option yet. An allowlist entry is the reviewed
  decision that the peer needs that fact.
- The unvalidated-message check counts a message with any `buf.validate` option as
  validated. Whether the rules are the right ones is review, and a message nested in
  a validated one still needs its own rule to leave the list. Rules on `cli.v1` are
  additive and carry only what a release already assumed; the retained store fixture
  (`cli/internal/store/filestore/testdata`) and `TestWhatEarlierBuildsSentStillPasses`
  hold that.
- Unenforced: whether a field is something the receiver needs, as opposed to
  how the sender works, needs judgment. Ask it in review for every new field.

## Release window

No `vibesys-v*` tag has been published, so the orchestrator's stored format and
`orchestrator/v1` are still free to change in place:

- `orchestrator/v1` is left out of the `buf breaking` run in
  `scripts/check_proto_breaking.sh` and is put back before the first tag. The
  release workflow refuses a tag until it is. `cli/v1` keeps its check, because
  the job tool has shipped; its changes are additive.
- The orchestrator store refuses a store written under an older schema, with an
  error that tells the operator to move the state directory away. It does not
  upgrade old rows. Run history on a development host is discarded by that move.
  The refusal is part of the store change that first needs it (see the parts
  below); until then the store reads its current schema only.

After the first tag, every change to either surface is a public or stored
format change and follows the compatibility rules of its own contract.

## The design

### The host and its control service

One host per state root. A client never learns about a host from a path, a
process list or a text file; it asks. The host serves a small control service
that stays compatible across releases and answers even when the orchestrator
cannot be served:

- `Identify` returns the release, how it relates to the asking client's release,
  when the host started, how many runs are live, whether each part it needs (the
  session holder, the skills, the job tool) is ready or why not, and whether the
  orchestrator is serving or why not (`HELD_ELSEWHERE`, `STATE_REFUSED`).
- `Stop` stops the host, optionally only when no runs are live. Sessions outlive
  the host, so stopping is safe for them.

The socket lives at a fixed place under the root. A host's settings come from a
`HostConfig` message in the root, not from the environment of whichever client
started it. The client starts a host with a minimal environment and the root as
its working directory. Parts are resolved on every launch, so repairing an
install does not need a restart. The client decides from `Identify`: same
release, use it; different release and no live runs, stop and start its own;
different and live runs, reads and steering work and a new run is refused with
a typed reason.

The host holds a lock file with its pid and release, read only by a forced stop
when the socket does not answer.

### The release

A release folder holds a `ReleaseManifest` message: version, platform, the
session protocol it speaks, the command that starts a session holder, the skills
directory, the job tool, and the files that must exist. The builder writes it,
the host reads it through generated code, and `vibesys-client release verify`
checks it. The installer asks that verb instead of keeping its own list of paths.
A host that cannot find a manifest reports a typed part problem rather than a
guess at a folder named `bin`.

### Host and session holder

`SessionBootstrap`, `OpenSessionRequest` and `SessionSkills` stay on the mechanism-word allowlist with a reason: the host decides where a session listens and works, so those paths are the facts the holder needs, not a detail of how it runs. The environment names in `production/sidecars.go` are listed until the environment codec gives each a message field.

A session holder is started with one binary message on stdin (where to listen
and its private state paths), and everything else arrives through typed calls
on one service: `Hello` (versions and which session answers), `Open`, `Send`,
`Follow`. `Open` carries the session, harness, model, working directory, an
opaque resume token, the skills bundle, the tool servers (so the tool-server
token never rests on disk) and an `AgentEnvironment` that the holder lays over
the login environment without interpreting it. There is no `launch.json`, no
`mcp.json` and no argv beyond the program name.

The host renders prompts from templates (`orchestrator/internal/prompts`, Go
`text/template`) and sends text: `SessionStartTurn` and `SessionSteer` carry a
`prompt` and no message. The holder needs words, not delivery bookkeeping, and
never learns a skill's name or what the words say. The stored effect keeps the
typed `Message` and the host renders when it sends, so a delivery that is
retried sends the same bytes. Names describe roles (`AgentSessionService`,
`SessionEvent`), not the current implementation.

### Process identity

A holder takes an exclusive lock on the file `SessionBootstrap.lock_file` names
for its whole life, before anything else, and replaces a `SessionProcesses`
message (its pid and the process groups of its agents) atomically at
`SessionBootstrap.processes_file`. The host, to find out whether a holder is
alive, tries the lock: if it gets it, the holder is dead and the groups are
reaped; if not, the pid is trustworthy because its writer holds the lock. No
`/proc` read, no text pid file, and no recycled pid is ever signalled. The
daemon and its supervisor already work this way.

Three consequences are held by tests:

- A second holder for a session cannot take the lock and exits (after a few short
  tries, because the host's own look at the lock lasts an instant), so a
  host that crashed between starting a holder and the holder's listening starts
  no second one: it sees the lock held and waits.
- A command to a holder whose lock is free is a lost session with a reason, not
  an `Unavailable` that a retry with backoff keeps asking about until a stream
  ends.
- A record that cannot be read ends nothing and is reported with its path.
  The pid in the record is signalled only while the lock is held; the groups
  only when it is free, and each only if its leader is gone or started when the
  record says (`ps -o lstart=` as an opaque token, no `/proc`), because a group
  id may have gone to another program since, as after a reboot.

### Environment contract

Every environment variable that one of our programs reads from another is a
field of a message with an `env_name` option naming the variable. One small
encoder and decoder turn the message into the environment and back, validate on
read, and name the variable when a value is invalid. The messages are
`AgentContext` (what `vibesys-cli` reads as flag defaults),
`ToolServerCredentials` (what only the tool server reads), `SkillEnvironment`
and `JobEnvironment` (what a job's commands may read). Resource-specific
variables stay with their resource as their own small message. Variables that
belong to external tools (`KUBECONFIG`, `HOME` for the agent harness) are not
ours to declare.

The agent's `HOME` and `XDG_*` are not rewritten to steer the job tool. The job
tool's place is one variable naming its runtime directory; `daemon ensure
--json` answers with the socket, and the directory records where its daemon
keeps state.

### Tools the agent calls

The tool server builds each tool's input schema from the message descriptors,
its protovalidate rules and a tool option on the message, so a field added to a
message reaches the agent and a missing field is refused before dispatch. Enum
spellings for people come from one helper. Result text comes from templates.
`Handoff` says what it means with a `oneof` of candidate or no candidate.

### What clients see

A stored record holds the public message plus its private part
(`AgentRecord`, `RunRecord`, `ReceiptRecord`), so a client's type cannot name a
private fact: no resume token, worktree path, job tool location or command
digest. Watchers receive `RunChange`, not the stored event. A tool call is a
`oneof` of a shell command or a named call with structured arguments; the
mapping from a harness's spelling happens in the normalizer, not the screen.

### Benchmark results and job environment

A benchmark's result records are a message whose protojson is the existing
result protocol, generated into both the Go reader and the SDK writer. The job
environment is `JobEnvironment`, and the SDK reads it from the generated code
instead of copying names.

### Records shared between our own programs

Anything one of our programs writes and another reads is a message: lease
holder files, node readings, task objectives (`TaskObjectives`, read from
`objectives.toml` through `tomlproto`). The few text sentinels our own
scripts print over `ssh` are confined to one package and tested.

The lease holder (`LeaseRecord`) and the node reading (`NodeReading`) are messages
in `shared.proto`, kept as protojson with the proto field names. Daemons of
different builds share these files during an upgrade, so:

- A record of a build from before the messages has the same keys and reads as
  one. `pid` is an `int32` so protojson writes a number, as the old readers expect.
- Readers discard unknown fields, so a field a later build adds does not stop an
  older build from reading the holder. A field is only added, and optional.
- The lock is the lease, never the record. A held lease whose record is empty or
  damaged is reported held by nobody nameable, never as free. `DecodeLeaseRecord`
  returns a named invalid-input failure and no holder for such a record.
- A node reading that is not one is treated as none kept: it is advisory.

`leasetest` holds every lease implementation to the first three.

## What is not built yet

Each row is a part of the design above that the code does not do yet, with the
boundaries in the inventory that it removes. The inventory baselines name the
part they wait for.

| Part | Not built | Removes |
| --- | --- | --- |
| Release manifest | `ReleaseManifest`, `release verify`, the host and installer reading it | hand path lists in the builder, installer and host; the `bin` folder rule |
| Host control | `HostControlService`, `HostConfig`, host lock, `host status/stop/restart`, stable socket, minimal-environment start, client policy | the `holder` text file, the `ps` scan, the hash in the socket path, `VIBESYS_ORCHESTRATOR_{STATE_DIR,SIDECAR,MARKETPLACE,CLI}` on the host |
| Environment codec | the `env_name` option, `AgentContext`, `ToolServerCredentials`, `SkillEnvironment` | the Python env table and its golden |
| Job tool endpoint | `JobToolEndpoint`, `daemon ensure --json`, `DaemonPlaces` | `CliLocation`, the `XDG_*` and `HOME` rewrite, the host's copy of the socket rule |
| Client records | `AgentRecord`, `RunRecord`, `ReceiptRecord`, `RunChange` | private fields served to clients |
| Tool calls | the `ToolCall` `oneof` | `args_json`, `provider_call_id`, the screen's `cmd`/`command` guess |
| Tool schemas | descriptors to JSON Schema, `Handoff` `oneof`, templates | hand schemas and decode structs in the tool server |
| Result records | `ResultRecord` generated for Go and the SDK | the hand parser and writer |
| Job environment | `JobEnvironment`, `ResultFormat` as a `oneof` | the SDK's manifest reader and name copies |
| Manifest 0.1 | `TaskManifestV01`: 48 example tasks and the Python input synthesis still write `version = "0.1"`, so it stays until they move | the hand TOML structs of the single-image reader |
| `cli.v1` validation | rules on the remaining data messages (jobs, results, outcomes, plan) and the responses; the allowlist lists them | the gap against the data rule |

## Boundaries and owners

"Owner" is the definition of the contract. "Typed" means it is already a
validated message; the other rows are what the parts above replace.

### Clients and host

| Boundary | Carrier | Owner | State |
| --- | --- | --- | --- |
| terminal client and desktop to host | gRPC `OrchestratorService` over a Unix socket | `orchestrator/v1/service.proto` | typed; run messages still mix stored and public fields |
| desktop renderer and Electron main | `DesktopGatewayRequest` and `Reply` bytes | `desktop/v1/gateway.proto` | typed |
| `connect` pipe | opaque bytes over stdio | carries the service above | typed |
| handshake | `Hello`, `BuildVersions`, `Compatibility` | `service.proto` | typed; the protocol revision is a file option read by every peer |
| client starts a host | argv and `HostStartReport` on a pipe; the host clears its environment | `host_control.proto` | typed |
| which root, which socket | `gatewayroot.Rule`, `lifecycle.SocketPath`; the desktop dials through the bridge | `gatewayroot` and `lifecycle` | one rule, held by tests |
| what a host is configured with | `ROOT/host-config.json`, a `HostConfig` | `host_config.proto`, a local file and not part of the frozen `host_control.proto` | typed |
| host absent or held | `HostStartReport` and `IdentifyResponse`, with a typed `Unserved` | `host_control.proto` | typed |
| installer to running host | process list match | none | untyped: host control |
| desktop to remote host | `sh -c` over `ssh` | desktop provisioning | external; tested |
| run list output for scripts | `run get --json`, protojson | `service.proto` | typed |
| client exit codes | 0 to 4 | `clientcli` | held by a test |
| store | SQLite rows of `Event` and `Effect` bytes | `events.proto`, `effects.proto` | typed |

### Host and session holder

| Boundary | Carrier | Owner | State |
| --- | --- | --- | --- |
| start | `SessionBootstrap` on stdin, then `Hello` and `Open` of `AgentSessionService` | `agent_session.proto` | typed |
| tool servers | `ToolServer` in `Open`; agentshim hands them to the provider through an inherited pipe | `agent_session.proto` | typed |
| pid and process groups | text files | hand copies | untyped: session processes |
| commands and events | gRPC `AgentSessionService` | `agent_session.proto` | typed |
| version check | `Hello`, ending in `INCOMPATIBLE_VERSIONS` | `agent_session.proto` | typed |
| why a session failed | `StartFailureReason` | `orchestrator.proto` | typed |
| prompts | `prompt` text in `SessionStartTurn` and `SessionSteer`, rendered by the host | `agent_session.proto`, `orchestrator/internal/prompts` | typed |
| agent harness | in-process library | agentshim | not a process boundary |
| harness and provider streams | stream-json and the provider's protocol | agentshim | external |

### Environment and tools

| Boundary | Carrier | Owner | State |
| --- | --- | --- | --- |
| identity into the job tool | `VIBESYS_{REPO,TASK,RUN,AGENT,HARNESS,CLI,PLACEMENT}` | a Python table and a golden | partly typed: environment codec |
| the job tool's place | `HOME` and `XDG_*` rewritten for the agent | `CliLocation` | leaks: job tool endpoint |
| tool server credentials | `VIBESYS_ORCHESTRATOR_*` | `daemon/ports.go` | untyped: environment codec |
| tool server to host | gRPC metadata for agent, session and token | `service.go` | typed |
| agent to tool server | MCP over stdio | MCP; framing confined to `internal/mcp` | external |
| tool arguments and results | hand JSON Schema, hand structs, formatted text | `tools.go` | two definitions: tool schemas |

### Job tool, daemon and supervisors

| Boundary | Carrier | Owner | State |
| --- | --- | --- | --- |
| host to daemon, read a task | gRPC `Hello`, `WatchTask` | `cli/v1/service.proto` | typed; the host never reads the daemon's files |
| job tool to daemon | gRPC `DaemonService` | `cli/v1/service.proto` | typed; few validation rules |
| daemon to supervisor | `launch.pb`, gRPC `SupervisorService`, `flock` on `alive` | `supervisor.proto` | typed; the model for process identity |
| daemon store files | protojson of `store.proto`, one owner | `store.proto` | typed |
| operator config and task manifest | TOML through `tomlproto` | `operator.proto`, `task.proto` | typed |
| objectives | TOML through `tomlproto` | `task.proto` (`TaskObjectives`) | typed |
| manifest 0.1 | hand TOML structs | `task` | untyped: manifest 0.1 |
| lease holder and node reading | JSON files shared across users and machines | `lease`, `apptainer` | untyped: lease and node records |
| agent to job tool | argv flags, `--json`, exit codes, `Failure` on stderr | `cmd/vibesys-cli` | typed output |

### Jobs and evaluators

| Boundary | Carrier | Owner | State |
| --- | --- | --- | --- |
| job places and candidate | `VIBESYS_ARTIFACTS_DIR`, `VIBESYS_CACHE_DIR`, `VIBESYS_SERVICE_URL`, `VIBESYS_MOUNTS` | several packages, copied in the SDK | untyped: job environment |
| benchmark result | result protocol 2, hand JSON on both sides | `sdk/vibesys-evaluator/PROTOCOL.md` | untyped: result records |
| load generator and phases | binary `PhaseInput`, `PhaseOutput`, `loadgen.proto` | `evaluator/v1` | typed |
| SDK to Go core | `Capabilities` as protojson | `loadgen.proto` | typed |

### Install and release

| Boundary | Carrier | Owner | State |
| --- | --- | --- | --- |
| bundle layout | path lists in the builder, installer and host | shell and `release.go` | untyped: release manifest |
| release index, checksums, asset names, installer settings | one token each, read by `sh` | `install.sh` | exempt, bootstrap |
| harness plugin manifest | `marketplace.json` | the agent harness | external; three readers today |

### External

The host reads `nvidia-smi` and `/proc` for machine statistics. The daemon runs
`docker`, `git`, `ssh`, `rsync`, Slurm, Apptainer, `kubectl`, `rocminfo` and
scans another user's daemon by process name for a status display. Each is
confined to one package with a Fake or recorded fixtures.
