// Package production is the wiring of the shipped binary: which stores, supervisors and
// environments its daemon and supervisor processes run on, and how a client starts the daemon.
//
// The wiring is the only thing here. The daemon, the supervisor and the client are packages of
// their own, composed from fakes in tests and from these in the binary, so nothing they do
// branches on which.
//
// This build ships no environment that can run a job: the host and docker environments arrive
// with the work that checks out and measures commits. Until then every job ends as not attempted,
// with that reason, and the daemon, the supervisors and the job lifecycle are complete.
package production
