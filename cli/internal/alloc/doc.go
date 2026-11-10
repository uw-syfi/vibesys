// Package alloc holds the pure rules of standing allocations: a slot of a placement's capacity that a
// run holds over time, and runs its jobs in one at a time, so they never wait for capacity again.
//
// It is the decision half of a pure core and a thin shell, like internal/lifecycle. The daemon feeds
// Step what happened (an agent asked for, ended or renewed an allocation, a job was submitted into
// one, started or finished, the backend was observed or could not be reached, time passed) and
// carries out the Actions it returns through the environment's Allocator: submit an allocation,
// release one, start a job in one, refuse a request with a typed reason, record how long one waited.
// Nothing here reads a clock, starts a goroutine or touches a file: now is an argument, so Fold
// replays a log of inputs to the same state and the same actions every time.
//
// The rules it holds:
//   - The states only move forward: Pending, Running, Draining, Ended, with Pending and Running also
//     going straight to Ended. Ended is final.
//   - An unreachable backend never ends an allocation; only an observation that the backend ended it,
//     or the run's own request, does.
//   - A job starts in an allocation only if it fits (Fit), one at a time, in the order the jobs were
//     queued; a job running when its allocation ends is lost, with the allocation's end reason.
//   - A run holds at most max_held allocations of a placement that are not Ended, renewals included.
//   - A renewal order makes at most one successor per allocation, never at or after its until, and
//     none once the run has ended.
//
// The files:
//   - types.go: the record of one allocation, its phases, and the operator's limits.
//   - backend.go: what the shell asks a backend for and what it observes there.
//   - events.go, actions.go: what Step takes and what it returns.
//   - refusal.go: why a request is refused, as typed values.
//   - fit.go: whether a job fits an allocation, and which allocation a job of any takes.
//   - step.go: Step and Fold.
package alloc
