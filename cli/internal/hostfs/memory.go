package hostfs

import (
	"context"
	"fmt"
	"io"
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
	exec    map[string]bool
	scratch map[string]bool
	dirs    map[string]bool
	next    int
}

var _ FS = (*Memory)(nil)

// NewMemory returns an empty file system.
func NewMemory() *Memory {
	return &Memory{files: map[string][]byte{}, exec: map[string]bool{}, scratch: map[string]bool{}, dirs: map[string]bool{}}
}

// MakeDir implements FS.
func (m *Memory) MakeDir(ctx context.Context, dir string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if !path.IsAbs(dir) {
		return fmt.Errorf("making a directory: %q is not an absolute path", dir)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.dirs[path.Clean(dir)] = true
	return nil
}

// Dirs returns the directories MakeDir made, sorted.
func (m *Memory) Dirs() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]string, 0, len(m.dirs))
	for d := range m.dirs {
		out = append(out, d)
	}
	sort.Strings(out)
	return out
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
	delete(m.exec, path.Clean(p))
}

// Files implements FS.
func (m *Memory) Files(root string) ([]File, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	root = path.Clean(root)
	if data, ok := m.files[root]; ok {
		return []File{{Size: int64(len(data)), Executable: m.exec[root]}}, nil
	}
	var out []File
	for p, data := range m.files {
		if rel, ok := strings.CutPrefix(p, strings.TrimSuffix(root, "/")+"/"); ok {
			out = append(out, File{Path: rel, Size: int64(len(data)), Executable: m.exec[p]})
		}
	}
	if len(out) == 0 && !m.dirs[root] {
		return nil, &fs.PathError{Op: "stat", Path: root, Err: fs.ErrNotExist}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out, nil
}

// ReadAt implements FS.
func (m *Memory) ReadAt(p string, b []byte, offset int64) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	data, ok := m.files[path.Clean(p)]
	switch {
	case !ok:
		return 0, &fs.PathError{Op: "open", Path: p, Err: fs.ErrNotExist}
	case offset < 0:
		return 0, fmt.Errorf("reading %s: negative offset %d", p, offset)
	case offset >= int64(len(data)):
		return 0, io.EOF
	}
	n := copy(b, data[offset:])
	if n < len(b) {
		return n, io.EOF
	}
	return n, nil
}

// WriteAt implements FS.
func (m *Memory) WriteAt(ctx context.Context, p string, data []byte, offset int64, executable bool) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if !path.IsAbs(p) {
		return fmt.Errorf("writing a file: %q is not an absolute path", p)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	p = path.Clean(p)
	have := m.files[p]
	if offset == 0 {
		have = nil
	}
	if int64(len(have)) < offset {
		return fmt.Errorf("writing a file: %s is shorter than the offset %d", p, offset)
	}
	out := append([]byte(nil), have[:offset]...)
	out = append(out, data...)
	if end := offset + int64(len(data)); end < int64(len(have)) {
		out = append(out, have[end:]...)
	}
	m.files[p] = out
	m.exec[p] = executable
	return nil
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
