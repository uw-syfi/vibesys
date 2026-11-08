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
| Environment reads | A read of a `VIBESYS_*` variable by its literal name (`os.Getenv`, `os.LookupEnv`, `getenv`, `os.environ`, `process.env`) anywhere but `cli/envcodec` fails. A read that is out of scope is listed with its exact count and a reason, and the list only shrinks. A name passed through a constant is not seen; the name check above still lists it. | `scripts/check_boundaries.py` |
| Mechanism words | Message, field, enum and rpc names in the orchestrator, `cli`, desktop and evaluator packages that contain a mechanism word (path, dir, file, pid, process, socket, json, sidecar, provider, worktree, marketplace, home, xdg, docker, sqlite, claude, codex) are in a shrink-only allowlist. | `scripts/check_boundaries.py` |
| Unvalidated messages | Every `cli.v1` message that has fields and no protovalidate rule is in a shrink-only allowlist. Empty messages are exempt. Each daemon and supervisor server validates the request of every RPC at its edge (`wire.RequestValidator`), and the store and run records validate on read, so a bad value is refused naming its field. | `scripts/check_boundaries.py` |
| Stored facts | No message a service of `orchestrator/v1` sends or receives can hold, at any depth, a message of `stored.proto` or a field marked `stored_only`. It reads the compiled descriptors, so a new field or nested message is held when it is declared. | `orchestrator_checks` (`orchestrator/internal/archcheck`, `StoredLeaks`) |
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

## After the first tag

The first `vibesys-v*` tag makes three things public. From then on a change to any of them
is a change to something a user already has.

- **`orchestrator/v1` is under `buf breaking`.** `scripts/check_proto_breaking.sh` holds it
  with `store/v1`, `search/v1`, `core/v1` and `cli/v1`: a deleted or renamed field, message,
  enum value or rpc fails the proto check. `scripts/tests/test_proto_breaking.sh` runs the
  real script against a deleted and a renamed field in each of those packages, so the orchestrator
  path is proved held and not only present in the script's text. The release workflow runs
  `scripts/check_boundaries.py --release`, which refuses a tag if the text ever loses that path.
  Only an additive change passes. A change that must break needs a new package version.
- **The stored format is schema 13.** The orchestrator's SQLite store records its schema
  version (`schemaVersion` in `orchestrator/internal/store/schema.go`). A host reads the
  version it was built with and the versions it lists as read (`readsVersions`: 12). It refuses a
  store of any other version, older or newer, naming the file and the versions, and it never
  converts a row. The message tells the operator to move the state directory away, which
  discards its run history. A store written before the tag has a version below 12 and is
  refused this way for good: only development builds wrote them. Version 12 is the first tag's
  format, and 13 added check-ins (an event body, a table and a message body) to it without
  changing what any version 12 row means, so a 13 build reads a 12 store as it is and only
  stamps 13 on it: the upgrade keeps run history and is one way. A build that reads only 12
  refuses that store by its number, with the same advice, and never replays an event body it
  cannot decode. From the tag on, a change that raises the number is a decision: either the new
  build reads the version before it (and stamps, if the version before cannot read what the new
  one writes), or it refuses on purpose and the release notes say that run history is lost. Do
  not raise the number without making that choice in the same change. An additive change to the
  stored bodies raises it for that reason too, because the build before cannot read them.
- **The client handshake says protocol 2.** `protocol_version` and `min_protocol_version` in
  `service.proto` are 2. Protocol 1 is the first protocol a published client speaks: it stayed at 1
  through the redesign before the tag because no older client was ever published. Protocol 2 is the
  host that serves what the desktop app now needs: `WatchRuns` with `detail = SUMMARY` and its
  `RunSummary` frames, and `GetRunHistory`. A host of protocol 1 ignores `detail` and lacks the call,
  so a client that needs them has to be refused, and the handshake is the only place that can. The one
  number is also the client's own floor: a client's `min_protocol_version` is the oldest host it still
  speaks, and a host answers `DAEMON_TOO_OLD` when that is above its own protocol (`protocol.Judge`).
  So raising `protocol_version` for a change a client depends on raises `min_protocol_version` with
  it, and the cost is that the host then refuses the clients below, with `CLIENT_TOO_OLD`. From now on a
  change that breaks a published client raises `protocol_version`, and `min_protocol_version` rises when
  a client starts to need the change or the host stops serving the clients below it.
  The session protocol between the host and the process that holds a session
  (`SESSION_PROTOCOL_VERSION_CURRENT` in `agent_session.proto`) is 2. The two sides always come
  from one bundle, so the number matters only to a session that outlived a host upgrade, and no
  published build wrote a 1; the value is part of the held contract and is not renumbered.

## The design

### The host and its control service

One host per state root. A client never learns about a host from a path, a
process list or a text file; it asks. The host serves a small control service
that stays compatible across releases and answers even when the orchestrator
cannot be served:

- `Identify` returns the release, how it relates to the asking client's release,
  when the host started, how many runs are live, whether each part it needs (the
  session holder, the skills, the job tool) is ready or why not, and whether the
  orchestrator is serving or why not (`HELD_ELSEWHERE`, `HOLDER_GONE`,
  `STATE_REFUSED`; a refused state directory also says whether it is on a network
  filesystem or one not known to be local, so a client names the setting that fixes
  it and does not parse the host's text). It also says how many other clients are
  connected and, for a build of no release, which program it is.
- `Stop` stops the host, optionally only when no runs are live. Sessions outlive
  the host, so stopping is safe for them.

The socket's place is a function of the root alone: `ROOT/host.sock`, or, for a
root too long for a socket address, a name derived from the root in a private
directory of the user under `/tmp`. A host's settings come from a `HostConfig`
message in the root, not from the environment of whichever client started it.
The client starts a host with a minimal environment and the root as its working
directory. Parts are resolved on every launch, so repairing an install does not
need a restart.

The client decides from `Identify` by one pure function, `clientcli.Decide`, whose
table is in `orchestrator/README.md`. Only `run start` and `host restart` replace
a host, and only after showing that a new one can start. No other verb stops a
host that serves. A newer host, or one with other clients, is not replaced unasked.

The host holds a lock file with its pid, release, root and machine. A client reads
it only while the lock is held, for a forced stop when the socket does not answer,
and to tell a host on another machine (a root on a shared disk) from no host at all:
that host is named and left alone, and its pid is never signalled.

### The release

A release folder holds a `ReleaseManifest` message: version, platform, the
command that starts a session holder, the skills directory, the job tool, and
the files that must exist. It also holds `catalogue.pb`, a `ReleaseCatalogue`
that lists the example tasks, and an `examples/` folder with each example's
files (see [Adding an example](examples.md)). The builder writes it,
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
field of a message with an `env_name` option naming the variable
(`proto/vibesys/cli/v1/env.proto`). One small encoder and decoder
(`cli/envcodec`) turn the message into the environment and back, validate on
read, and name the variable when a value is invalid. A variable keeps its name,
so `$VIBESYS_RUN` and `$VIBESYS_CLI` still read from a shell. The messages are
`AgentContext` (the run's identity, which `vibesys-cli` reads as flag defaults),
`OperatorContext` (`VIBESYS_PLACEMENT`, which the operator sets and the
orchestrator leaves alone), `SkillEnvironment` (`VIBESYS_CLI`, which only the
skills' shell commands read) and `ToolServerCredentials` (what only the tool
server reads), `JobEnvironment` (what a job's commands may read: where its
artifacts and cache are, the candidate's address, its time limit and the
read-only data sets) and `KubernetesEnvironment` (the cluster a job that needs
one is given). Resource-specific variables stay with their resource as their own
small message. `KUBECONFIG` is a field of `KubernetesEnvironment` because
`vibesys-cli` writes it, under the name `kubectl` reads; `HOME` for the agent
harness is not ours to declare.

How a value is written: a string is itself, an unsigned integer is its decimal
digits, and an enum is its value's name without the enum's prefix, in lower
case, and a repeated string is its elements separated by commas, so an element
holds no comma. A field at its zero value is not set, and a variable that is unset or
empty reads as the zero value, which the field's protovalidate rules then allow
or refuse. A refusal names the variable and never repeats the value, because
one of them is a secret. The writer holds a message to the same rules before it
writes, so a sender cannot write what a reader would refuse.

A message may declare an `env_namespace`, the prefix of the names it owns.
The decoder reports a name in that prefix that no field declares and does not
refuse it: the environment is inherited, and a name in the prefix can be
another program's setting. The tool server prints the report on its standard
error. This is the rule for unknown keys applied where refusing would break a
session that has everything it needs.

The session's token is on the tool server's environment only: it is a field of
`ToolServerCredentials`, which rides in `OpenSessionRequest.tools[].env`, and a
test holds it out of the agent's own environment and out of every argument.

The agent's `HOME` and `XDG_*` are not rewritten to steer the job tool. The job
tool's place is one variable naming its runtime directory, a field of
`JobToolPlace`. The tool answers `daemon ensure --json` with
`DaemonEnsureResponse`, the socket and the directory, so the host holds no rule
for either. The directory records where its daemon keeps state in
`DaemonPlaces`, written before the first daemon starts there, and any later
start reuses it.

A start asks the tool before the command is decided, and a start the tool cannot
answer is refused in the tool's words. A run records `JobToolEndpoint`, the
program and the directory. A process told a directory that records nothing does
not start a daemon: its environment is not the one that chose that daemon, so it
exits 4 and says so. `daemon ensure` is the one command that sets a directory up
again, which is what the host's follower does after a reboot. The host keeps
`XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and
`VIBESYS_CLI_CONFIG` from its starter: they are what its `daemon ensure` resolves
the operator's daemon, store and settings from, so that the host and the
operator's own shell meet one daemon.

### Tools the agent calls

A message of the contract is a tool's input when it carries the `mcp_tool`
option (`tool_options.proto`): `SpawnAgentInput`, `ListAgentsInput`,
`SendMessage`, `StopAgent`, `Handoff` and `Report`. The tool server reads that
message's descriptor. Its fields are the arguments, a `tool_field_doc` option is
what the agent reads about each, a `tool_field_hidden` option keeps a field the
server fills (`Report.sequence`) out of the schema and refuses it in a call, and
the protovalidate rules say which arguments are required and which enum values
are allowed. A call is read into the message, so an unknown key, a value of the
wrong kind, a missing required argument and a bad enum value are each refused
before anything is sent, in one answer that names every one. The schema uses no
`oneOf` or `anyOf`, which a client may refuse at the top of a schema: a group of
which one field is to be given says so in its description and the validation
holds it.

`Handoff` has the fields every handoff has and a `oneof` body with one message
per role (`worker`, `planner`, `judge`, `combiner`, `profiler`); a worker's body
is itself a `oneof` of a candidate or why it has none. The body's field is named
for the role as the enum spells it (`names.Spell`), so the server finds the
caller's own body without a table, and a handoff with none or with another
role's is refused in the tool's answer, naming the field to fill, before
anything is sent. The core holds the same line for a call that reaches the host
another way: it refuses a handoff whose body is not its poster's role with
`REFUSAL_HANDOFF_NOT_YOURS` and posts nothing. Enum
spellings for people and agents (`worker`, `claude_code`) come from the enum
alone (`internal/names`); the desktop app derives its harness choices from the
generated enum. What the server says back (a started agent, a delivery, the list
of agents, each refusal) comes from templates under `internal/mcp/templates/`,
which receive data and nothing else. The server registers and names itself
`vibesys` (`mcp.ServerName`), and a test holds the harness skill to it. A golden
of `tools/list` for the lead and for a worker pins every word an agent reads of
the schemas.

### What clients see

A stored record holds the public message plus its private part
(`AgentRecord`, `RunRecord`, `ReceiptRecord`), so a client's type cannot name a
private fact: no resume token, job tool location or command digest. The records
and the stored `Event` live in `stored.proto`, which the TypeScript bindings leave
out. Watchers receive `RunChange`, the public part of each event, and a fold of
those over a snapshot holds what a later snapshot would say. A working copy kept
after a run (`Run.kept_checkouts`) is public on purpose: the operator decides what
becomes of it, and the client shows where it is. A tool call is a
`oneof` of what a client shows (a command, a file read, write or edit, a search) or
`OtherCall`, a tool's own name and the text of what it was asked, so an unknown tool is shown
and never lost. The mapping from a harness's tool and argument names is
`vs_sidecar/tool_use.py`, the one place that knows them, and not the screen. It stays there
and not in agentshim because it needs no provider fact that is not already in the events agentshim
emits (a tool's name and its arguments); moving it upstream later changes only that module.

### Making a run

The calls a client makes while its operator fills in a new run are read-only
asks of the host, except provisioning. All are for the operator only; an agent
never calls them.

- `ListCatalogue` returns the example tasks of the release (from its
  `catalogue.pb`), each with its card and a `Fit`. `ListEvalPlaces` returns the
  places a run's candidates can be evaluated on and the hardware each gives. The
  host asks the job tool's daemon for the places (`ListPlacements`) and for
  every card (`DescribeTask`), so it reads no task file and no hardware itself.
  A `Fit` is advice worked out per call from the current places; `StartRun`
  decides again, and refuses a place that is unknown or does not give the
  task's accelerator.
- `ProvisionRepository` makes a git repository on the host, from a catalogue
  example or empty, and never overwrites. It is idempotent on its request id. A
  refusal is a typed `ProvisionRefusal`, not an error. `CheckRepository` and
  `ListDirectory` let a client check and complete a typed repository path
  before it sends `StartRun`.
- `Dispatch` carries `UpdateRun`, which changes a live run's cost limit, time
  limit, fanout limit or lead, all or none (a limit can be removed; lowering the
  fanout stops no agent).
- A run may begin in a setup phase: `BeginSetup` starts a chat with a lead that
  makes the run's task, and the lead names the task with the `declare_task`
  tool (the `DeclareTask` command, setup and lead only). The host never trusts
  the lead's word that the task is ready: it reads the draft through the job
  tool daemon's `DescribeSetup` (declarations, task version, head commit, jobs
  and base measurements) and derives six checks from those facts, which clients
  see in `RunSummary.setup`. `CompleteSetup` is refused with
  `REFUSAL_SETUP_INCOMPLETE` unless the host's own derivation passes all six.
  Setup spend and time are reported apart and do not count against the run's
  limits; those start when the run leaves setup. A run recorded before phases
  reads as optimizing.

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
| Result records | `ResultRecord` generated for Go and the SDK | the hand parser and writer |
| Manifest 0.1 | `TaskManifestV01`: 48 example tasks and the Python input synthesis still write `version = "0.1"`, so it stays until they move | the hand TOML structs of the single-image reader |
| `cli.v1` validation | rules on the remaining data messages (jobs, results, outcomes, plan) and the responses; the allowlist lists them | the gap against the data rule |

## Boundaries and owners

"Owner" is the definition of the contract. "Typed" means it is already a
validated message; the other rows are what the parts above replace.

### Clients and host

| Boundary | Carrier | Owner | State |
| --- | --- | --- | --- |
| terminal client and desktop to host | gRPC `OrchestratorService` over a Unix socket | `orchestrator/v1/service.proto` | typed |
| desktop renderer and Electron main | `DesktopGatewayRequest` and `Reply` bytes | `desktop/v1/gateway.proto` | typed |
| `connect` pipe | opaque bytes over stdio | carries the service above | typed |
| handshake | `Hello`, `BuildVersions`, `Compatibility` | `service.proto` | typed; the protocol revision is a file option read by every peer |
| client starts a host | argv and `HostStartReport` on a pipe; the host clears its environment | `host_control.proto` | typed |
| which root, which socket | `gatewayroot.Rule`, `lifecycle.SocketPath`; the desktop dials through the bridge | `gatewayroot` and `lifecycle` | one rule, held by tests |
| what a host is configured with | `ROOT/host-config.json`, a `HostConfig` | `host_config.proto`, a local file and not part of the frozen `host_control.proto` | typed |
| host absent or held | `HostStartReport` and `IdentifyResponse`, with a typed `Unserved` | `host_control.proto` | typed |
| installer to running host | `vibesys-client host stop --if-no-live-runs`, read by exit code | `clientcli` | typed |
| desktop to remote host | `sh -c` over `ssh` | desktop provisioning | external; tested |
| run list output for scripts | `run get --json`, protojson | `service.proto` | typed |
| client exit codes | 0 to 4 | `clientcli` | held by a test |
| store | SQLite rows of `Event` and `Effect` bytes | `events.proto`, `effects.proto` | typed |
| catalogue, eval places, fit | `ListCatalogue`, `ListEvalPlaces` | `catalogue.proto`, `eval_place.proto` | typed |
| a new repository | `ProvisionRepository` | `provision.proto` | typed |
| a typed repository path | `CheckRepository`, `ListDirectory` | `service.proto` | typed |
| a live run's settings, and setup | `Dispatch` of `UpdateRun`, `BeginSetup`, `CompleteSetup` | `events.proto`, `setup.proto` | typed |

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
| identity into the job tool | `VIBESYS_{REPO,TASK,RUN,AGENT,HARNESS,CLI,PLACEMENT}` | `AgentContext`, `OperatorContext`, `SkillEnvironment` (`env.proto`) | typed |
| the job tool's place | `VIBESYS_CLI_RUNTIME_DIR` | `JobToolPlace` in `cli/v1/env.proto` | typed |
| tool server credentials | `VIBESYS_ORCHESTRATOR_*` | `ToolServerCredentials` (`tool_server.proto`) | typed |
| tool server to host | gRPC metadata for agent, session and token | `service.go` | typed |
| agent to tool server | MCP over stdio | MCP; framing confined to `internal/mcp` | external |
| tool arguments and results | JSON Schema and checks generated from the input messages, answers from templates | the `mcp_tool` messages (`tool_options.proto`) | typed |

### Job tool, daemon and supervisors

| Boundary | Carrier | Owner | State |
| --- | --- | --- | --- |
| host to daemon, read a task | gRPC `Hello`, `WatchTask` | `cli/v1/service.proto` | typed; the host never reads the daemon's files |
| job tool to daemon | gRPC `DaemonService` | `cli/v1/service.proto` | typed; few validation rules |
| daemon to supervisor | `launch.pb`, gRPC `SupervisorService`, `flock` on `alive` | `supervisor.proto` | typed; the model for process identity |
| daemon store files | protojson of `store.proto`, one owner | `store.proto` | typed |
| host to daemon, a task's card, a draft's setup facts, the places | gRPC `DescribeTask`, `DescribeSetup`, `ListPlacements` | `cli/v1/service.proto` | typed; the daemon is the only reader of task files and git |
| the release's examples | `catalogue.pb` and `examples/` in the release folder | `release_catalogue.proto` | typed |
| operator config and task manifest | TOML through `tomlproto` | `operator.proto`, `task.proto` | typed |
| objectives | TOML through `tomlproto` | `task.proto` (`TaskObjectives`) | typed |
| manifest 0.1 | hand TOML structs | `task` | untyped: manifest 0.1 |
| lease holder and node reading | JSON files shared across users and machines | `lease`, `apptainer` | untyped: lease and node records |
| agent to job tool | argv flags, `--json`, exit codes, `Failure` on stderr | `cmd/vibesys-cli` | typed output |

### Jobs and evaluators

| Boundary | Carrier | Owner | State |
| --- | --- | --- | --- |
| job places, candidate, limit and data sets | `JobEnvironment`, `KubernetesEnvironment` as variables | `env.proto`; `cli/envcodec`, and the SDK's codec held to the same corpus | typed |
| benchmark result format | `ResultFormat`, a `oneof` (plans written before it still read) | `supervisor.proto` | typed |
| benchmark result | result protocol 2, hand JSON on both sides | `sdk/vibesys-evaluator/PROTOCOL.md` | untyped: result records |
| load generator and phases | binary `PhaseInput`, `PhaseOutput`, `loadgen.proto` | `evaluator/v1` | typed |
| SDK to Go core | `Capabilities` as protojson | `loadgen.proto` | typed |

### Install and release

| Boundary | Carrier | Owner | State |
| --- | --- | --- | --- |
| bundle layout | `release.json`, a `ReleaseManifest` | `release.proto` | typed |
| release index, checksums, asset names, installer settings | one token each, read by `sh` | `install.sh` | exempt, bootstrap |
| harness plugin manifest | `marketplace.json` | the agent harness | external; three readers today |

### External

The host reads `nvidia-smi` and `/proc` for machine statistics. The daemon runs
`docker`, `git`, `ssh`, `rsync`, Slurm, Apptainer, `kubectl`, `rocminfo` and
scans another user's daemon by process name for a status display. Each is
confined to one package with a Fake or recorded fixtures.
