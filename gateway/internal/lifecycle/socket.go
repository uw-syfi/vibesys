package lifecycle

import (
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"syscall"
)

var ErrServing = errors.New("gateway already serving")

// SocketPath is shared by serve and connect. Root is the host's .vibesys
// directory; schemaHash is the generated API hash.
func SocketPath(root, schemaHash string) string {
	return filepath.Join(root, "gateway", schemaHash, "vibesysd.sock")
}

// Endpoint owns the listener and the per-hash process lock. The lock file is
// retained across exits: unlinking it would allow different inodes to be locked.
type Endpoint struct {
	Listener *net.UnixListener
	lock     *os.File
}

func Listen(root, schemaHash string) (*Endpoint, error) {
	path := SocketPath(root, schemaHash)
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	for _, dir := range []string{filepath.Dir(directory), directory} {
		if err := os.Chmod(dir, 0700); err != nil {
			return nil, err
		}
	}
	lock, err := os.OpenFile(filepath.Join(directory, "vibesysd.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	release := true
	defer func() {
		if release {
			lock.Close()
		}
	}()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, ErrServing
		}
		return nil, err
	}
	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return nil, fmt.Errorf("refusing to replace non-socket %q", path)
		}
		if err := os.Remove(path); err != nil {
			return nil, err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(path, 0600); err != nil {
		listener.Close()
		return nil, err
	}
	release = false
	return &Endpoint{Listener: listener, lock: lock}, nil
}

func (e *Endpoint) Close() error {
	return errors.Join(e.Listener.Close(), e.lock.Close())
}
