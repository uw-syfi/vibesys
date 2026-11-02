// Package lifecycle holds the pure rules of the daemon's job lifecycle: which queued job runs
// next, what a finished command means for its job, what recovery does with a job found running
// after a restart, and when a wait is over.
//
// It is the decision half of a pure core and a thin shell. The daemon (internal/app) feeds it
// events (a job was admitted, a launch finished, a supervisor reported an outcome, a supervisor
// vanished) and carries out the requests it returns through the interfaces that own the I/O: the
// store, the environment and the supervisors. State plus an event gives new state plus requests,
// and nothing here reads a clock, starts a goroutine or touches a file, so the whole lifecycle,
// recovery included, is tested over generated event sequences.
//
// Like internal/core it imports no I/O. It is a separate package because it names the daemon's
// own concepts (a launch, an outcome, a slot on the machine), which are not part of the shared
// domain rules.
//
//   - ids.go: Ref, the identity of one job across every store the daemon serves.
//   - launch.go: Launch and Outcome, what a supervisor is given and what it reports.
//   - machine.go: State and Step, the scheduler and the recovery table.
//   - settle.go: Settle, the transition that an outcome means.
//   - await.go: Await, when a client's wait is over.
package lifecycle
