// Package production is the wiring of the shipped binary: which stores, supervisors and
// environments its daemon and supervisor processes run on, and how a client starts the daemon.
//
// The wiring is the only thing here. The daemon, the supervisor and the client are packages of
// their own, composed from fakes in tests and from these in the binary, so nothing they do
// branches on which.
//
// Jobs run on the host or, for a task with a Dockerfile, in docker.
package production
