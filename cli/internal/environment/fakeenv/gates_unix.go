package fakeenv

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"syscall"
)

// FIFOGates are gates that live in a directory, so a script running in another process waits for
// a test running in this one. A gate is a named pipe and a file beside it. The script blocks reading
// the pipe, and a release creates the file and then writes a byte to the pipe. Nothing polls.
//
// The file is what makes a release last. A pipe keeps a byte only while someone has it open, so a
// byte written before the script gets there is gone when the writer closes. A waiter opens the pipe
// first and looks for the file second: a release that finished before it opened left the file, and
// one that did not will write to a pipe the waiter holds. Either way the waiter sees it.
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

func (g *FIFOGates) marker(name string) string { return filepath.Join(g.dir, name+".released") }

// Release opens the gate for everyone who waits at it, now and later, as MemoryGates does.
func (g *FIFOGates) Release(name string) error {
	if err := os.WriteFile(g.marker(name), nil, 0o600); err != nil {
		return err
	}
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
	defer f.Close()
	if _, err := os.Stat(g.marker(name)); err == nil {
		return true
	}
	released := make(chan bool, 1)
	go func() {
		var b [1]byte
		n, err := f.Read(b[:])
		if n == 1 && err == nil {
			// Whoever else waits needs a byte too.
			_, err = f.Write(b[:])
		}
		released <- n == 1 && err == nil
	}()
	select {
	case ok := <-released:
		return ok
	case <-ctx.Done():
		// Closing the file ends the blocked read, which is how the reader goroutine ends.
		return false
	}
}
