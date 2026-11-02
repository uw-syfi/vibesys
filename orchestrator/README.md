# Orchestrator

The orchestrator is a Go daemon that runs on the host doing the work, usually a remote one. It
launches Claude Code and Codex agents, records everything they do, relays typed messages between
them, and wakes a lead agent when a subagent or a job finishes. It sits beside the Python runs and
shares nothing with them: it has its own contract under `proto/vibesys/orchestrator/v1/`, its own
module here, and it reaches `vibesys-cli` only through that tool's public contract.

Work is landing in stages. This module so far holds the contract, the pure decision core, the
interface over `vibesys-cli` jobs, and the code that converts between the two. There is no daemon
yet, and no store, sidecar, MCP server or desktop client.

## Vocabulary

- **Run:** one optimization of one task. It keeps its id across restarts.
- **Agent:** one agent in a tree under the run's lead. It keeps its id across resumes.
- **Session:** one process of an agent. Each resume is a new session with a higher number.
- **Sidecar:** the process that holds a session. It outlives the daemon, so a daemon restart does
  not stop an agent.
- **Turn:** one unit of work by an agent, started by one message.
- **Item:** one ordered entry in a turn, changed in place from running to finished.
- **Message:** typed mail between agents and the operator: handoffs, reports, steering.
- **Effect:** something the core asks the outside world to do, kept in a durable outbox until done.

## Layers

| Package | Holds | May import |
| --- | --- | --- |
| `internal/core` | State, `Decide`, the role table. No I/O, clock or randomness | Nothing of this module |
| `internal/jobs` | `JobSource`, the interface over `vibesys-cli` jobs | `core` |
| `internal/jobs/jobstest` | The in-memory `JobSource` and the contract every one passes | `core`, `jobs` |
| `internal/wire` | Bytes to core types and back, once, at the edge | `core`, `gen` |
| `gen/` | Generated from the `.proto` files. Do not edit | Nothing of this module |

`internal/archcheck` enforces this table and the purity of the core from the import graph of every
package, along with the rule that this module imports no other module of the repository.

## How a decision is made

Everything that happens to the system is one input to one function:

```go
func Decide(s State, in Input) (State, []Event, []Effect)
```

An input is a command, a sidecar fact, a job update or a timer firing. It carries its time and a
nonce, so the function reads no clock and mints ids with no randomness. The shell commits the events
and the effects in one transaction, then carries out the effects and feeds what comes of them back
in as inputs.

Every state change goes through one fold over events, so a state rebuilt from the log with `Replay`
equals the live one by construction, and the simulation checks that it does.

What this gives the daemon to come:

- A command is answered once. A retry with the same id gets the same receipt, and the same id on a
  different command is a conflict. A refusal is remembered as well, across a restart.
- An effect id is stable, so a repeat from the outbox is recognised and dropped by whoever carries
  it out. Delivery is at least once, with no duplicates seen by the agent.
- A sidecar fact counts once. A stream resumed from the last committed offset changes nothing it
  already changed, and a fact from a session that has been replaced changes nothing at all.
- No wait is open forever. A session that does not answer a stop is written off by a timer, a
  subagent that stops without a handoff tells its lead, and retries are bounded.

## Testing

`./check.sh` runs formatting, `go vet` and every test.

The core is tested at three levels, all in one thread with no real time:

- `decide_test.go` names one scenario per behavior.
- `simulation_test.go` runs whole runs against a model of everything around the core: a daemon that
  crashes and replays its log through stored bytes, an outbox that repeats effects, sidecars that
  outlive the daemon or die on their own, clients that retry, and a job source that repeats itself.
- `monitor_test.go` checks the guarantees from the committed log, with its own books, so it shares
  no code with the core: no agent runs two turns at once, fan-out stays within its limit, each
  message is taken exactly once, the replayed state equals the live one, and every run and message
  ends when everything stops. It also counts what each simulation reached, and fails a property that
  was never exercised.

One seed fixes a whole simulation. A failure prints the seed and the command that replays it:

```sh
ORCHESTRATOR_SIM_SEED=123 go test ./internal/core -run 'TestSimulation$'
```

To search further than the default 40 seeds:

```sh
ORCHESTRATOR_SIM_SEEDS=400 go test ./internal/core -run 'TestSimulation$' -timeout 10m
```

`internal/wire` builds random messages from the `.proto` descriptors, so a field added to the
contract is tested with no change to the tests, and a mapper that forgets it fails its round trip.
Its fuzz targets take untrusted bytes; `go test` runs their seeds, and this runs one for real:

```sh
go test ./internal/wire -run '^$' -fuzz '^FuzzParseCommand$' -fuzztime 30s
```

## Generation

The code in `gen/` comes from `proto/vibesys/orchestrator/v1/` through `buf.gen.orchestrator.yaml`,
and `scripts/generate_proto.sh` regenerates it with the rest of the repository's generated code.
