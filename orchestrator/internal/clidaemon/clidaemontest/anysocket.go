package clidaemontest

import (
	"context"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
)

// AnySocket is an Ensurer whose daemon is always there, for a test about what a follower does with the
// connection it gets.
type AnySocket struct{ Err error }

// Ensure implements clidaemon.Ensurer.
func (a AnySocket) Ensure(context.Context) (clidaemon.Socket, error) {
	if a.Err != nil {
		return clidaemon.Socket{}, a.Err
	}
	return clidaemon.SocketFrom("/run/test/daemon.sock")
}
