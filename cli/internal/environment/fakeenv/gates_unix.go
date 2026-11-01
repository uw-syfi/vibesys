package fakeenv

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"syscall"
)

// FIFOGates are gates that live in a directory, so a script running in another process waits for
// a test running in this one. A gate is a named pipe: the script blocks reading it, and a gate
// is released when a byte is written to it. Nothing polls.
type FIFOGates struct {
	dir string
	mu  sync.Mutex
}

var _ Gates = (*FIFOGates)(nil)

// NewFIFOGates returns gates under dir, which must exist.
func NewFIFOGates(dir string) *FIFOGates { return &FIFOGates{dir: dir} }

// Dir is the directory the gates live in.
func (g *FIFOGates) Dir() string { return g.dir }

func (g *FIFOGates) open(name string) (*os.File, error) {
	path := filepath.Join(g.dir, name)
	g.mu.Lock()
	err := syscall.Mkfifo(path, 0o600)
	g.mu.Unlock()
	if err != nil && !os.IsExist(err) {
		return nil, err
	}
	// Read-write, so opening does not wait for the other end and a released gate stays released
	// until someone has read the byte.
	return os.OpenFile(path, os.O_RDWR, 0)
}

// Release opens the gate. A gate released before anyone waits stays released, since the byte is
// kept in the pipe.
func (g *FIFOGates) Release(name string) error {
	f, err := g.open(name)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = f.Write([]byte{1})
	return err
}

// Wait implements Gates. It reports false when ctx is done first.
func (g *FIFOGates) Wait(ctx context.Context, name string) bool {
	f, err := g.open(name)
	if err != nil {
		return false
	}
	released := make(chan bool, 1)
	go func() {
		var b [1]byte
		n, err := f.Read(b[:])
		released <- n == 1 && err == nil
	}()
	select {
	case ok := <-released:
		f.Close()
		return ok
	case <-ctx.Done():
		// Closing the file ends the blocked read, which is how the reader goroutine ends.
		f.Close()
		return false
	}
}
