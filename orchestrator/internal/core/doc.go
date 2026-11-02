// Package core holds the orchestrator's decisions as one pure function:
//
//	Decide(State, Input) (State, []Event, []Effect)
//
// Everything that happens reaches it as an Input: a command, a fact from a sidecar, a job update,
// a fired timer. It returns the facts to commit and the I/O to do. A thin shell commits both in one
// transaction, then carries out the effects at least once and feeds what comes back as new input.
// Nothing here reads a clock, draws a random number, starts a goroutine or touches a file: time and
// a fresh nonce for ids arrive in the Input.
//
// Every change to State goes through one fold, so Decide's result and Replay of its events are the
// same state by construction, and a restart is a replay. Terminal turns and settled messages leave
// State once the store holds them, so its size follows what is live and not what happened.
//
// The package imports nothing of this module. internal/wire parses the contract into these types
// and encodes them back.
//
//   - ids.go: identifier types.
//   - vocabulary.go: closed sets of states and reasons.
//   - model.go: what is recorded: runs, agents, turns, items, messages, receipts.
//   - input.go: Input, and the commands, facts and updates it can carry.
//   - output.go: Event and Effect.
//   - roles.go: the one table of who may do what.
//   - state.go: State, its fold, Replay and what the shell may read.
//   - decide.go: Decide and the commands.
//   - delivery.go: routing messages, starting turns, stopping agents and finishing runs.
//   - sidecar.go: facts from sidecars, jobs and timers.
//   - invariants.go: what holds after every step.
package core
