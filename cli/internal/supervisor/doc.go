// Package supervisor is the process that owns one job's command, and the daemon's view of it.
//
// A job runs under its own supervisor so that it outlives a restart of the daemon: the daemon
// records a job as running, a supervisor runs the command in its own process group, and a daemon
// that comes back finds the supervisor and carries on. The package has two halves that meet at
// an interface:
//
//   - Supervisor is the object a supervisor process runs. It is parked until Start, runs the
//     command in an environment, keeps the output in a Spool and the outcome in memory, and
//     lives until Release. It reports facts and decides nothing: what an outcome means for the
//     job is lifecycle.Settle.
//   - Supervisors and Handle are what the daemon uses to reach supervisors, wherever they run.
//     InProcess is an implementation that runs them in the daemon's own process, for tests of
//     everything above. internal/supervisor/rpc and internal/supervisor/host are the one that
//     runs each in a process of its own. All of them pass the contract suite in suptest.
package supervisor
