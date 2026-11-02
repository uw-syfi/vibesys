// Package host runs each supervisor in a process of its own, and finds the ones left over.
//
// A run is a directory per job under a root on local disk: the launch the daemon wrote down, the
// supervisor's socket, the spool of its output, and the supervisor's own stderr. Host is the
// daemon's side: it writes the launch, starts a detached supervisor process on the directory and
// dials its socket. Serve is the supervisor's side: it is what the process runs.
//
// The supervisor is started in a session of its own, so it keeps running when the daemon that
// started it is stopped, killed or restarted.
package host
