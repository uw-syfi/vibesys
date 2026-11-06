package lifecycle

import (
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"syscall"

	"github.com/uw-syfi/vibesys/orchestrator/privatesocket"
)

const (
	socketFileName = "host.sock"
	lockFileName   = "host.lock"
	logFileName    = "host.log"

	// MaxSocketPath is the longest socket path a root gets to keep inside itself.
	MaxSocketPath = privatesocket.MaxPath
)

var ErrServing = errors.New("gateway already serving")

// SocketPath is the one socket of the host of root, a function of root alone: a client that starts
// a host and a client that finds one compute the same path, whatever their environments. It is
// ROOT/host.sock, unless that does not fit in a socket address. Then it is a name derived from root
// in the private directory of this user (privatesocket.Directory).
func SocketPath(root string) string {
	inside := filepath.Join(root, socketFileName)
	if privatesocket.Fits(inside) {
		return inside
	}
	return privatesocket.Short(root)
}

// CheckSocketDirectory refuses a socket whose directory another user could have made; see
// privatesocket.CheckDirectory.
func CheckSocketDirectory(socket string) error { return privatesocket.CheckDirectory(socket) }

// LogPath is where a host's stderr goes. It is appended to, so whoever starts a host can read what
// that one attempt wrote and nothing older.
func LogPath(root string) string { return filepath.Join(root, logFileName) }

// Held is a root whose lock this process holds, and whose socket it has not published yet. The lock
// file is retained across exits: unlinking it would allow different inodes to be locked.
type Held struct {
	root string
	lock *os.File
}

// Hold takes the lock of root. It is ErrServing when another process holds it.
func Hold(root string) (*Held, error) {
	if err := os.MkdirAll(root, 0700); err != nil {
		return nil, err
	}
	lock, err := os.OpenFile(filepath.Join(root, lockFileName), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, ErrServing
		}
		return nil, err
	}
	return &Held{root: root, lock: lock}, nil
}

// Record writes what a reader of the lock file finds while the lock is held: who holds it. It
// replaces whatever an earlier holder left.
func (h *Held) Record(data []byte) error {
	if err := h.lock.Truncate(0); err != nil {
		return err
	}
	if _, err := h.lock.WriteAt(data, 0); err != nil {
		return err
	}
	return h.lock.Sync()
}

// Close releases the lock.
func (h *Held) Close() error { return h.lock.Close() }

// Publish starts listening on the root's socket, replacing one an earlier holder left behind. The
// Endpoint owns the lock from here on.
func (h *Held) Publish() (*Endpoint, error) {
	path := SocketPath(h.root)
	if err := privatesocket.EnsureDirectory(path); err != nil {
		return nil, err
	}
	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return nil, fmt.Errorf("refusing to replace non-socket %q", path)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	// The socket's mode is what guards it, since a root may be readable by others.
	listener, err := privatesocket.Listen(path)
	if err != nil {
		return nil, err
	}
	return &Endpoint{Listener: listener, held: h, path: path}, nil
}

// Endpoint owns the listener and the root's lock.
type Endpoint struct {
	Listener *net.UnixListener
	held     *Held
	path     string
}

// Listen holds root and publishes its socket, for what has nothing to do in between.
func Listen(root string) (*Endpoint, error) {
	held, err := Hold(root)
	if err != nil {
		return nil, err
	}
	endpoint, err := held.Publish()
	if err != nil {
		held.Close()
		return nil, err
	}
	return endpoint, nil
}

func (e *Endpoint) Close() error {
	// The name goes while the lock is held, so it can never be another host's.
	return errors.Join(e.Listener.Close(), privatesocket.Remove(e.path), e.held.Close())
}

// Holder reads the record of the process holding the lock of root. held is false when no process
// holds the lock, whatever the file says: a record left by a process that has since exited names
// nobody, and the number it holds may belong to another program now. The probe takes and releases
// the lock when it is free.
func Holder(root string) (record []byte, held bool, err error) {
	file, err := os.Open(filepath.Join(root, lockFileName))
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	defer file.Close()
	switch err := syscall.Flock(int(file.Fd()), syscall.LOCK_SH|syscall.LOCK_NB); {
	case err == nil:
		return nil, false, nil
	case !errors.Is(err, syscall.EWOULDBLOCK):
		return nil, false, err
	}
	record, err = io.ReadAll(file)
	return record, true, err
}
