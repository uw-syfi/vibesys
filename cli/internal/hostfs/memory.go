package hostfs

import (
	"context"
	"fmt"
	"io/fs"
	"path"
	"sort"
	"strings"
	"sync"
)

// Memory is a file system in memory, for a simulated host. Its scratch directories are
// /memory/scratch-N, and a file can be put anywhere. It is safe for concurrent use.
type Memory struct {
	mu      sync.Mutex
	files   map[string][]byte
	scratch map[string]bool
	next    int
}

var _ FS = (*Memory)(nil)

// NewMemory returns an empty file system.
func NewMemory() *Memory {
	return &Memory{files: map[string][]byte{}, scratch: map[string]bool{}}
}

// MakeScratch implements FS.
func (m *Memory) MakeScratch(ctx context.Context) (string, func(), error) {
	if err := ctx.Err(); err != nil {
		return "", nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.next++
	dir := fmt.Sprintf("/memory/scratch-%d", m.next)
	m.scratch[dir] = true
	return dir, func() { m.remove(dir) }, nil
}

func (m *Memory) remove(dir string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.scratch, dir)
	for p := range m.files {
		if strings.HasPrefix(p, dir+"/") {
			delete(m.files, p)
		}
	}
}

// ReadFile implements FS.
func (m *Memory) ReadFile(p string, limit int64) ([]byte, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	data, ok := m.files[path.Clean(p)]
	if !ok {
		return nil, &fs.PathError{Op: "open", Path: p, Err: fs.ErrNotExist}
	}
	if int64(len(data)) > limit {
		return nil, ErrTooLarge
	}
	return append([]byte(nil), data...), nil
}

// Put writes a file, replacing what was there.
func (m *Memory) Put(p string, data []byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.files[path.Clean(p)] = append([]byte(nil), data...)
}

// Scratch returns the scratch directories that exist, sorted: a test that expects none left
// behind checks it is empty.
func (m *Memory) Scratch() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]string, 0, len(m.scratch))
	for d := range m.scratch {
		out = append(out, d)
	}
	sort.Strings(out)
	return out
}
