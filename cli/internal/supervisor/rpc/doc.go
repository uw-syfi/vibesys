// Package rpc carries the supervisor interfaces over gRPC: Server puts a supervisor.Supervisor
// on a listener, and Client is a supervisor.Handle that reaches one.
//
// The contract is proto/vibesys/cli/v1/supervisor.proto. A call that cannot complete returns a
// status whose details carry a Failure; a connection that is gone is the status Unavailable
// without one, which the client reports as supervisor.ErrGone.
package rpc
