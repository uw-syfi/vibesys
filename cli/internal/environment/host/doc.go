// Package host is the environment.Environment that runs commands directly on this machine, in a
// clean checkout the caller made.
//
// Every command, and the service of an execution, runs as the leader of a process group of its
// own, and the group ends on every path: the leader exiting, the limit, a cancelled context, a
// service that never became ready, an error, and the death of the process that called Run. The
// last one holds because each leader is started by a small shell guard that watches a pipe whose
// write end only the caller holds; when the caller dies, however it dies, the pipe closes and the
// guard kills the group. A process that leaves the group on its own (setsid) is out of reach of
// any such rule and is not covered.
//
// The package does I/O and holds no policy: what to run, for how long, and what an outcome means
// are decided by the callers and by core. Time enters only through the clock.Timers it is given,
// and every goroutine it starts is joined before Run returns.
package host
