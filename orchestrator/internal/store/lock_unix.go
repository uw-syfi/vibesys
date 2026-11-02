//go:build unix

package store

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// lockDir takes the one-writer lock of a state directory and returns what releases it. A second
// daemon on the same directory is refused here, since SQLite alone would let two processes write.
func lockDir(dir string) (release func(), err error) {
	path := filepath.Join(dir, "LOCK")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("store: open %s: %w", path, err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, fmt.Errorf("store: %s is in use by another process (one orchestrator owns a state directory): %w", dir, err)
	}
	return func() { f.Close() }, nil
}
