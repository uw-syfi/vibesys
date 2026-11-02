package fsys

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"sync"
	"syscall"
)

// Lock implements FS with flock(2) on a file opened for writing.
//
// The kernel drops the hold when the process dies, so a crash leaves nothing to clean up. On NFS,
// Linux turns flock into a whole-file lock on the server, which holds across machines, and which
// the server lets go of when a crashed client's lease runs out (NFSv4, about 90 seconds) or when it
// reboots (NFSv3). Until then the file reads as locked, which is the safe way to be wrong.
//
// Where a lock would keep nobody out it fails with ErrNoLocks and takes no hold: when the
// filesystem refuses the call, when a second open of the file can take the lock too, and on an NFS
// mount with local_lock or nolock, whose locks stop at the machine.
func (o OS) Lock(name string) (Lock, error) {
	flock := o.Flock
	if flock == nil {
		flock = syscall.Flock
	}
	f, err := os.OpenFile(name, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, err
	}
	if err := try(flock, f); err != nil {
		_ = f.Close()
		return nil, &fs.PathError{Op: "lock", Path: name, Err: err}
	}
	if err := excludes(flock, name, f); err != nil {
		_ = f.Close()
		return nil, &fs.PathError{Op: "lock", Path: name, Err: err}
	}
	return &osLock{f: f}, nil
}

// try takes the hold or says why not.
func try(flock func(int, int) error, f *os.File) error {
	for {
		err := flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		switch {
		case err == nil:
			return nil
		case errors.Is(err, syscall.EINTR):
		case errors.Is(err, syscall.EWOULDBLOCK):
			return ErrLocked
		case errors.Is(err, syscall.ENOLCK), errors.Is(err, syscall.ENOTSUP), errors.Is(err, syscall.ENOSYS):
			return fmt.Errorf("%w: %w", ErrNoLocks, err)
		default:
			return err
		}
	}
}

// excludes proves the hold on f means something. Holds belong to an open of a file and not to a
// process, so a second open here must be refused just as another process would be.
func excludes(flock func(int, int) error, name string, f *os.File) error {
	second, err := os.OpenFile(name, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	defer func() { _ = second.Close() }()
	switch err := try(flock, second); {
	case err == nil:
		return fmt.Errorf("%w: a second open of the file took the same lock", ErrNoLocks)
	case !errors.Is(err, ErrLocked):
		return err
	}
	return checkMount(f)
}

type osLock struct {
	once sync.Once
	f    *os.File
	err  error
}

// Release implements Lock. Closing the only open of the file ends the hold.
func (l *osLock) Release() error {
	l.once.Do(func() { l.err = l.f.Close() })
	return l.err
}
