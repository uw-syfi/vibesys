package hostfs

import (
	"bytes"
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
	links   map[string]string
	scratch map[string]bool
	dirs    map[string]bool
	next    int
}

var _ FS = (*Memory)(nil)

// NewMemory returns an empty file system.
func NewMemory() *Memory {
	return &Memory{files: map[string][]byte{}, links: map[string]string{}, scratch: map[string]bool{}, dirs: map[string]bool{}}
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
	for p := range m.links {
		if strings.HasPrefix(p, dir+"/") {
			delete(m.links, p)
		}
	}
}

// PutLink makes a symbolic link at p to target, replacing what was there. Tree lists it as a link
// and Open refuses it, and every path through it, as the real file system's do.
func (m *Memory) PutLink(p, target string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	p = path.Clean(p)
	delete(m.files, p)
	m.links[p] = target
}

// Tree implements FS.
func (m *Memory) Tree(ctx context.Context, dir string) ([]Entry, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if !path.IsAbs(dir) {
		return nil, fmt.Errorf("listing a directory: %q is not an absolute path", dir)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	prefix := path.Clean(dir) + "/"
	var out []Entry
	for p, data := range m.files {
		if rel, ok := strings.CutPrefix(p, prefix); ok && !m.underLink(p) {
			out = append(out, Entry{Path: rel, Kind: EntryFile, Size: int64(len(data))})
		}
	}
	for p := range m.links {
		if rel, ok := strings.CutPrefix(p, prefix); ok && !m.underLink(p) {
			out = append(out, Entry{Path: rel, Kind: EntrySymlink})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out, nil
}

// underLink says a parent of p is a link, which hides what is put under it, as a walk that does
// not follow links never reaches it.
func (m *Memory) underLink(p string) bool {
	for d := path.Dir(p); d != "/" && d != "."; d = path.Dir(d) {
		if _, ok := m.links[d]; ok {
			return true
		}
	}
	return false
}

// Open implements FS.
func (m *Memory) Open(dir, rel string) (io.ReadCloser, error) {
	if !path.IsAbs(dir) || rel == "" || path.IsAbs(rel) || path.Clean(rel) != rel || rel == ".." || strings.HasPrefix(rel, "../") {
		return nil, fmt.Errorf("opening %q in %q: %w", rel, dir, ErrNotRegular)
	}
	p := path.Join(dir, rel)
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, link := m.links[p]; link || m.underLink(p) {
		return nil, fmt.Errorf("opening %q in %q: %w", rel, dir, ErrNotRegular)
	}
	data, ok := m.files[p]
	if !ok {
		return nil, &fs.PathError{Op: "open", Path: p, Err: fs.ErrNotExist}
	}
	return io.NopCloser(bytes.NewReader(append([]byte(nil), data...))), nil
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
	delete(m.links, path.Clean(p))
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
