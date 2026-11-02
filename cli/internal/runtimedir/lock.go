package runtimedir

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sync"
	"syscall"
)

// ErrHeld says the lock is held by someone else.
var ErrHeld = errors.New("the lock is held by another process")

// Lock is a held advisory lock on a file. The operating system drops it when the process dies,
// however it dies, so a lock is never left behind by a crash.
type Lock struct{ f *os.File }

// TryLock takes the lock on the file, creating it if need be, or fails with ErrHeld at once.
func TryLock(path string) (*Lock, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, ErrHeld
		}
		return nil, fmt.Errorf("locking %s: %w", path, err)
	}
	return &Lock{f: f}, nil
}

// WaitLock takes the lock on the file once the holder lets go. The wait ends with ctx's error if
// ctx is done first; a lock that is then taken in the background is dropped at once.
//
// The wait is not polled and is bounded by the holder, who holds this lock only while it starts
// the daemon.
func WaitLock(ctx context.Context, path string) (*Lock, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	taken := make(chan error, 1)
	var (
		mu    sync.Mutex
		state = waiting
	)
	go func() {
		err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX)
		mu.Lock()
		defer mu.Unlock()
		if state == abandoned {
			// Nobody waits for this lock any more: dropping the file drops it.
			f.Close()
			return
		}
		state = delivered
		taken <- err
	}()
	finish := func(err error) (*Lock, error) {
		if err != nil {
			f.Close()
			return nil, fmt.Errorf("locking %s: %w", path, err)
		}
		return &Lock{f: f}, nil
	}
	select {
	case err := <-taken:
		return finish(err)
	case <-ctx.Done():
		mu.Lock()
		if state == delivered {
			// The lock came in as the context ended; it is the caller's, and they see ctx done.
			mu.Unlock()
			return finish(<-taken)
		}
		state = abandoned
		mu.Unlock()
		return nil, ctx.Err()
	}
}

// The states of a WaitLock: the lock is awaited, was handed to the waiter, or the waiter left.
const (
	waiting = iota
	delivered
	abandoned
)

// Unlock drops the lock.
func (l *Lock) Unlock() error { return l.f.Close() }
