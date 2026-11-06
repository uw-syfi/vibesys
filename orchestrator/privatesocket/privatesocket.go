// Package privatesocket listens on a Unix socket only the user can reach, from the moment it has its
// name. A socket's mode can be set before it has a name only through the umask, which belongs to the
// whole process: while it is narrowed, a directory any other goroutine creates comes out with no
// execute bit, and two windows that overlap can leave it narrowed for good. So a socket is bound in a
// private directory of its own, given its mode there, and renamed into place.
package privatesocket

import (
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync/atomic"
)

// bindName is the socket's name in the private directory. With the directory's name (".b", a process
// id of at most 7 digits, "-" and a count of a digit or two) it adds at most 15 bytes to the
// directory of the socket, so a path that fits with a name of 9 bytes or more, as the host's and the
// agent socket do under their 100-byte limit, fits while it is bound too.
const bindName = "s"

// binds numbers the sockets this process has bound, so that two bound at once in one directory, as
// the host's socket and the agent socket are in the short private directory, never share a name.
var binds atomic.Uint32

// Listen listens on path, replacing what a crash left there, with mode 0600. The caller must own path,
// by holding the lock of whatever it belongs to. Closing the listener does not remove path: the caller removes it, with Remove.
func Listen(path string) (*net.UnixListener, error) {
	private := filepath.Join(filepath.Dir(path), fmt.Sprintf(".b%d-%d", os.Getpid(), binds.Add(1)))
	if err := os.RemoveAll(private); err != nil {
		return nil, err
	}
	if err := os.Mkdir(private, 0o700); err != nil {
		return nil, err
	}
	defer os.RemoveAll(private)
	bound := filepath.Join(private, bindName)
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: bound, Net: "unix"})
	if err != nil {
		return nil, fmt.Errorf("listen on %s: %w", path, err)
	}
	listener.SetUnlinkOnClose(false)
	err = os.Chmod(bound, 0o600)
	if err == nil {
		err = os.Rename(bound, path)
	}
	if err != nil {
		return nil, errors.Join(fmt.Errorf("listen on %s: %w", path, err), listener.Close())
	}
	return listener, nil
}

// Remove takes the socket's name away once its listener is closed. A name already gone is fine.
func Remove(path string) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}
