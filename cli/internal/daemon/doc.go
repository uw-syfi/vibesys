// Package daemon is the per-user daemon as a process: the DaemonService that clients call, and Run,
// which holds the instance lock, recovers, serves a private Unix socket, and stops cleanly.
//
// The job flows are internal/app's. This package only translates the contract's messages to
// them and back, and owns the process's resources: the lock, the socket and the shutdown. What
// the daemon runs on (stores, supervisors, environments) is passed in as a Runtime, so the shipped
// binary and a test compose it differently.
package daemon
