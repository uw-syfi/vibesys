//go:build unix

// Package locallease keeps the leases of this machine as advisory locks (flock) on files of one
// directory that every user's daemon shares, which runtimedir.ResolveLeases finds on local disk. The operating
// system gives a lock back when the process that holds it dies, however it dies, so a crashed
// holder never blocks anyone; a lease of this machine is never lost while its daemon lives.
//
// The directory is made sticky and writable by everyone, as /tmp is, so each user's daemon can add
// the lock of a resource nobody leased before, and nobody can remove or replace another's. A lock
// file is opened without following a link, and never created over one that is there: on a system
// that protects files in sticky directories (fs.protected_regular), opening another user's file
// to create it is refused, and opening it as it is is not.
//
// The holder of a lease writes who it is into the lock file while it holds it, and clears it when
// it gives the lease back, so whoever finds the lock held can say by whom.
package locallease

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"syscall"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
)

// Dir is the leases of this machine, kept in a directory.
type Dir struct {
	// Path is the directory. It is made, sticky and writable by everyone, if it is not there.
	Path string
}

var _ lease.Leases = Dir{}

// Where implements lease.Leases.
func (d Dir) Where() string { return "this machine (" + d.Path + ")" }

// Acquire implements lease.Leases.
func (d Dir) Acquire(ctx context.Context, key lease.Key, holder core.LeaseHolder) (lease.Lease, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if _, err := lease.ParseKey(string(key)); err != nil {
		return nil, err
	}
	if err := d.ensure(); err != nil {
		return nil, err
	}
	f, err := open(d.file(key))
	if err != nil {
		return nil, unavailable("opening the lease %s in %s: %v", key, d.Path, err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		defer f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, &lease.HeldError{Key: key, Where: d.Where(), Holder: readHolder(f)}
		}
		return nil, unavailable("locking the lease %s in %s: %v", key, d.Path, err)
	}
	if err := writeHolder(f, holder); err != nil {
		f.Close()
		return nil, unavailable("writing the holder of the lease %s in %s: %v", key, d.Path, err)
	}
	return &Lease{key: key, f: f}, nil
}

// file is the lock file of a key.
func (d Dir) file(key lease.Key) string { return filepath.Join(d.Path, string(key)+".lock") }

// ensure makes the directory if it is not there, sticky and writable by everyone.
func (d Dir) ensure() error {
	if _, err := os.Stat(d.Path); err == nil {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(d.Path), 0o755); err != nil {
		return unavailable("making the directory of leases %s: %v", d.Path, err)
	}
	if err := os.Mkdir(d.Path, 0o755); err != nil && !errors.Is(err, os.ErrExist) {
		return unavailable("making the directory of leases %s: %v", d.Path, err)
	} else if err == nil {
		// The mode is set after the directory is made, as the process's umask leaves it.
		if err := os.Chmod(d.Path, os.ModeSticky|0o777); err != nil {
			return unavailable("opening the directory of leases %s to every user: %v", d.Path, err)
		}
	}
	return nil
}

// open opens a lock file, making it if it is not there and never following a link. A file another
// user made is opened as it is, read-only if it is not writable: a lock needs only an open file.
func open(path string) (*os.File, error) {
	for range 3 {
		f, err := os.OpenFile(path, os.O_RDWR|syscall.O_NOFOLLOW, 0)
		if errors.Is(err, os.ErrPermission) {
			f, err = os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
		}
		if err == nil {
			return regular(f)
		}
		if !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		f, err = os.OpenFile(path, os.O_RDWR|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o666)
		if errors.Is(err, os.ErrExist) {
			// Another daemon made it first: open theirs.
			continue
		}
		if err != nil {
			return nil, err
		}
		// Every user's daemon writes its name into the lock it holds, whatever this one's umask.
		if err := f.Chmod(0o666); err != nil {
			f.Close()
			return nil, err
		}
		return f, nil
	}
	return nil, fmt.Errorf("%s keeps appearing and disappearing", path)
}

// regular refuses anything but a regular file, such as a fifo that would block a read.
func regular(f *os.File) (*os.File, error) {
	info, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, err
	}
	if !info.Mode().IsRegular() {
		f.Close()
		return nil, fmt.Errorf("%s is not a regular file", f.Name())
	}
	return f, nil
}

// recordLimit bounds what is read of a lock file.
const recordLimit = 4096

func writeHolder(f *os.File, h core.LeaseHolder) error {
	b, err := json.Marshal(lease.RecordOf(h))
	if err != nil {
		return err
	}
	if err := f.Truncate(0); err != nil {
		if errors.Is(err, os.ErrPermission) || errors.Is(err, syscall.EBADF) || errors.Is(err, syscall.EINVAL) {
			// A lock file another user made without letting others write it: the lock holds, and
			// whoever finds it held is told nobody's name.
			return nil
		}
		return err
	}
	_, err = f.WriteAt(append(b, '\n'), 0)
	return err
}

// readHolder reads the holder of a held lock. A holder that has not written its name yet, or that
// cannot, is the zero holder.
func readHolder(f *os.File) core.LeaseHolder {
	b, err := io.ReadAll(io.LimitReader(io.NewSectionReader(f, 0, recordLimit), recordLimit))
	if err != nil {
		return core.LeaseHolder{}
	}
	var r lease.Record
	if err := json.Unmarshal(bytes.TrimSpace(b), &r); err != nil {
		return core.LeaseHolder{}
	}
	return r.Holder()
}

// Lease is a lease of this machine: an open lock file.
type Lease struct {
	key  lease.Key
	mu   sync.Mutex
	f    *os.File
	done bool
}

var _ lease.Lease = (*Lease)(nil)

// Release implements lease.Lease. It clears the holder's name, then closes the file, which gives
// back the lock.
func (l *Lease) Release(context.Context) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.done {
		return nil
	}
	l.done = true
	_ = l.f.Truncate(0)
	return l.f.Close()
}

// Lost implements lease.Lease. A lease of this machine is held as long as its process lives.
func (*Lease) Lost() <-chan struct{} { return nil }

// Key implements lease.Lease.
func (l *Lease) Key() lease.Key { return l.key }

func unavailable(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "lease: " + fmt.Sprintf(format, args...)}
}
