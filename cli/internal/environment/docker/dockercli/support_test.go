//go:build docker && unix

package dockercli_test

import (
	"path/filepath"
	"strings"
	"sync"
)

// syncWriter is an output writer that is safe to read after the run.
type syncWriter struct {
	mu sync.Mutex
	b  *strings.Builder
}

func (w *syncWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.b.Write(p)
}

type discard struct{}

func (discard) Write(p []byte) (int, error) { return len(p), nil }

func evalSymlinks(p string) (string, error) { return filepath.EvalSymlinks(p) }
