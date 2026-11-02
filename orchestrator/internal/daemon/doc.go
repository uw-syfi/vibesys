// Package daemon is the orchestrator's shell: the one process per user per host that feeds every
// input to the decision core, commits what it decides, and carries out the effects.
//
// The package starts no goroutine, reads no clock and opens no socket. Time, nonces, the sidecars,
// the job source and the plan reader come in through the interfaces in ports.go, and the work is
// done in steps, so a test or a simulation runs a whole daemon in one thread:
//
//   - Engine (engine.go) is the serial loop. Every input passes through Engine.Apply, which
//     decides, commits through the store, and only then publishes to watchers. A mutex orders
//     inputs; what it guards is small and does no I/O but the store.
//   - Daemon (daemon.go) is Engine plus the effect worker, the sidecar streams and the job poll.
//     Step does one pass of all of them and Run repeats it on a tick.
//   - Service (service.go) is OrchestratorService over the Engine and the hub.
//
// A command from a client reaches the Engine through Service. Everything else reaches it through
// Step. A restart is Open: recover the state, reattach the sidecars that still run, and let the
// outbox deliver what was not delivered.
package daemon
