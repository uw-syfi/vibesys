// Package clidaemon owns the CLI daemon link. The tool itself says where its daemon is; a Session
// publishes only complete resets and retains its last readable view.
package clidaemon

import (
	"fmt"
	"path/filepath"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// Unreadable is a link failure. Detail is for display, never state inference.
type Unreadable struct {
	Reason core.ViewReason
	Detail string
}

func (e *Unreadable) Error() string { return e.Detail }

// Socket is where a daemon answers, as the tool that ensured it said. Only SocketFrom makes one, so a
// connection never starts from a path this program put together.
type Socket struct{ path string }

// Path is the socket's path.
func (s Socket) Path() string { return s.path }

// SocketFrom is the socket a tool's answer names, which must be an absolute path.
func SocketFrom(answered string) (Socket, error) {
	if !filepath.IsAbs(answered) {
		return Socket{}, fmt.Errorf("the socket %q is not an absolute path", answered)
	}
	return Socket{answered}, nil
}
