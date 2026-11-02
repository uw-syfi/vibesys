package runtimedir

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// maxSocketPath is the longest Unix socket path every supported system accepts, with room for
// the terminating byte. macOS allows 104 bytes and Linux 108.
const maxSocketPath = 100

// FS answers what only the operating system knows about a path.
type FS interface {
	// IsNetwork reports whether the path is on a network file system.
	IsNetwork(path string) (bool, error)
}

// System is the FS of this machine.
func System() FS { return systemFS{} }

// Resolve returns where the runtime directory is for a user: a directory named for the tool under
// XDG_RUNTIME_DIR when it is set to an absolute path, and otherwise a private directory named
// for the user under /tmp, which is local on every system the tool supports.
func Resolve(getenv func(string) string, uid int) string {
	if dir := getenv("XDG_RUNTIME_DIR"); dir != "" && filepath.IsAbs(dir) {
		return filepath.Join(dir, "vibesys-cli")
	}
	return fmt.Sprintf("/tmp/vibesys-cli-%d", uid)
}

// Dir is an opened runtime directory.
type Dir struct{ path string }

// Open makes the directory if it is missing and checks it: it must be a real directory (not a
// link someone else planted), owned by the user, private to them, and on local disk. A directory
// that is the user's but too open is made private. The paths in it must fit in a Unix socket
// address.
func Open(path string, uid int, fsys FS) (Dir, error) {
	if !filepath.IsAbs(path) {
		return Dir{}, invalid("the runtime directory %q must be an absolute path", path)
	}
	if err := os.MkdirAll(path, 0o700); err != nil {
		return Dir{}, fmt.Errorf("creating the runtime directory: %w", err)
	}
	info, err := os.Lstat(path)
	if err != nil {
		return Dir{}, err
	}
	if !info.IsDir() {
		return Dir{}, invalid("the runtime directory %q is not a directory (a link is not accepted)", path)
	}
	if st, ok := info.Sys().(*syscall.Stat_t); !ok || int(st.Uid) != uid {
		return Dir{}, invalid("the runtime directory %q is not owned by the current user", path)
	}
	if info.Mode().Perm() != 0o700 {
		if err := os.Chmod(path, 0o700); err != nil {
			return Dir{}, fmt.Errorf("making the runtime directory private: %w", err)
		}
	}
	network, err := fsys.IsNetwork(path)
	if err != nil {
		return Dir{}, fmt.Errorf("checking where the runtime directory is: %w", err)
	}
	if network {
		return Dir{}, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"the runtime directory %q is on a network file system, where sockets and locks are not reliable; "+
				"set XDG_RUNTIME_DIR to a directory on local disk", path)}
	}
	d := Dir{path: path}
	if n := len(d.Socket()); n > maxSocketPath {
		return Dir{}, invalid("the daemon socket path %q is %d bytes, over the %d a Unix socket allows; "+
			"set XDG_RUNTIME_DIR to a shorter directory", d.Socket(), n, maxSocketPath)
	}
	if err := os.MkdirAll(d.Supervisors(), 0o700); err != nil {
		return Dir{}, fmt.Errorf("creating the supervisor directory: %w", err)
	}
	return d, nil
}

func invalid(format string, args ...any) *core.Failure {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

// ReadyFD is the file descriptor on which a client that starts the daemon passes the readiness
// pipe, and ReadyByte is what the daemon writes to it once it serves. ReadyTaken is what it writes
// instead when another daemon holds the machine. A daemon that fails first closes the pipe with
// neither, so the client learns of the failure when it happens and not after some wait.
const (
	ReadyFD    = 3
	ReadyByte  = 'R'
	ReadyTaken = 'T'
)

// ErrDaemonRunning says another daemon holds the machine.
var ErrDaemonRunning = errors.New("a daemon is already running")

// Path is the directory itself.
func (d Dir) Path() string { return d.path }

// Socket is where the daemon listens.
func (d Dir) Socket() string { return filepath.Join(d.path, "daemon.sock") }

// DaemonLock is the file whose lock the one running daemon holds.
func (d Dir) DaemonLock() string { return filepath.Join(d.path, "daemon.lock") }

// Log is the file a daemon started by a client writes its own diagnostics to.
func (d Dir) Log() string { return filepath.Join(d.path, "daemon.log") }

// StartLock is the file whose lock a client holds while it starts the daemon, so that clients
// that find no daemon at once start one.
func (d Dir) StartLock() string { return filepath.Join(d.path, "start.lock") }

// Supervisors is the directory of the supervisors' run directories.
func (d Dir) Supervisors() string { return filepath.Join(d.path, "sup") }
