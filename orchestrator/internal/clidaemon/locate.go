// Package clidaemon owns the CLI daemon link. Discovery checks precede dialing;
// a Session publishes only complete resets and retains its last readable view.
package clidaemon

import (
	"fmt"
	"io/fs"
	"path/filepath"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// Unreadable is a link failure. Detail is for display, never state inference.
type Unreadable struct {
	Reason core.ViewReason
	Detail string
}

func (e *Unreadable) Error() string { return e.Detail }

// Directory is Lstat metadata, including ownership and locality, supplied by FS.
type Directory struct {
	Mode    fs.FileMode
	UID     int
	Network bool
}

// FS inspects the socket directory without following a final symlink.
type FS interface {
	LstatDirectory(string) (Directory, error)
}

// Location is the recorded environment shared with the run's agent processes.
type Location struct{ XDGRuntimeDir, XDGStateHome, Home string }

// Socket is proof that discovery checked the directory. Only Locate creates it.
type Socket struct{ path string }

func (s Socket) Path() string { return s.path }

// Locate follows the CLI discovery golden. It creates and repairs nothing.
func Locate(loc Location, uid int, fsys FS) (Socket, error) {
	// The runtime directory retains the vibesys-cli name for compatibility.
	dir := fmt.Sprintf("/tmp/vibesys-cli-%d", uid)
	if filepath.IsAbs(loc.XDGRuntimeDir) {
		dir = filepath.Join(loc.XDGRuntimeDir, "vibesys-cli")
	}
	socket := filepath.Join(dir, "daemon.sock")
	fail := func(detail string) (Socket, error) { return Socket{}, &Unreadable{core.NoDaemon, dir + ": " + detail} }
	if len(socket) > 100 {
		return fail("socket path exceeds 100 bytes")
	}
	info, err := fsys.LstatDirectory(dir)
	if err != nil {
		return fail(err.Error())
	}
	if !info.Mode.IsDir() || info.Mode&fs.ModeSymlink != 0 {
		return fail("not a real directory")
	}
	if info.UID != uid {
		return fail("directory belongs to another user")
	}
	if info.Mode.Perm() != 0700 || info.Mode&(fs.ModeSetuid|fs.ModeSetgid|fs.ModeSticky) != 0 {
		return fail("directory mode must be 0700")
	}
	if info.Network {
		return fail("directory is on a network file system")
	}
	return Socket{socket}, nil
}
