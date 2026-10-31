// Package machinestatstest scripts a machine for the sampler's tests. It holds
// real tool output recorded on Linux and macOS.
package machinestatstest

import (
	"context"
	"fmt"
	"path/filepath"
	"sync"
	"sync/atomic"

	"github.com/uw-syfi/vibesys/gateway/internal/machinestats"
)

// FakeHost is a scripted machine, as strict as SystemHost about what exists: a
// tool or file is present only if listed. Each read of a file takes its next
// content and stays on the last. A tool is a function of its argv, which must
// return once ctx ends, as SystemHost's Run does.
type FakeHost struct {
	Tools map[string]func(ctx context.Context, args []string) ([]byte, error)
	Files map[string][][]byte

	// Calls counts ReadFile and Run calls, the machine's load from sampling.
	Calls atomic.Int64

	mu    sync.Mutex
	reads map[string]int
}

// ReadFile returns the file's next scripted content.
func (h *FakeHost) ReadFile(path string) ([]byte, error) {
	h.Calls.Add(1)
	h.mu.Lock()
	defer h.mu.Unlock()
	contents, ok := h.Files[path]
	if !ok || len(contents) == 0 {
		return nil, fmt.Errorf("open %s: no such file", path)
	}
	if h.reads == nil {
		h.reads = map[string]int{}
	}
	index := min(h.reads[path], len(contents)-1)
	h.reads[path]++
	return contents[index], nil
}

// Find resolves a tool by its bare name, then by a listed full path.
func (h *FakeHost) Find(tool string, candidates ...string) (string, error) {
	if _, ok := h.Tools[tool]; ok {
		return tool, nil
	}
	for _, candidate := range candidates {
		if _, ok := h.Tools[candidate]; ok {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("%s: %w", tool, machinestats.ErrToolNotFound)
}

// Run replies as the tool at path, which must have come from Find.
func (h *FakeHost) Run(ctx context.Context, path string, args ...string) ([]byte, error) {
	h.Calls.Add(1)
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	run, ok := h.Tools[path]
	if !ok {
		return nil, fmt.Errorf("exec %s: no such tool", filepath.Base(path))
	}
	return run(ctx, args)
}
