package fsys

import (
	"errors"
	"fmt"
	"io/fs"
	"maps"
	"path"
	"slices"
	"strings"
	"sync"
)

// ErrCrashed is what every call returns once the process has died.
var ErrCrashed = errors.New("fsys: the process crashed")

// ErrInjected is what a call fails with when a fault says it fails.
var ErrInjected = errors.New("fsys: injected failure")

// FaultKind is what happens at a write step.
type FaultKind int

// The fault kinds. A step is one call that changes the filesystem: Mkdir, Create, Write, Sync,
// Rename, Remove, SyncDir or Lock. Reads, Close and Release are not steps.
const (
	// FaultCrash stops the process before the step: the step does not happen and every later call
	// fails with ErrCrashed. CrashImage then says what a restart finds.
	FaultCrash FaultKind = iota + 1
	// FaultTornWrite stops the process in the middle of a Write: half of its bytes were written.
	// At any other step it is FaultCrash.
	FaultTornWrite
	// FaultFail fails the step with ErrInjected and leaves the filesystem as it was. Later calls
	// work.
	FaultFail
	// FaultFailAfter makes the step happen and then fails it with ErrInjected, as a call that
	// timed out after the server had done the work. Later calls work.
	FaultFailAfter
)

// Fault names the step to fail, counting from the first step of the MemFS at 0. It fires once.
type Fault struct {
	At   int
	Kind FaultKind
}

// MemFS is an in-memory filesystem that models what a crash keeps. Every file has the content
// its writers wrote and the content a Sync made durable; every directory has its entries and the
// entries a SyncDir made durable. A process crash keeps all of it, and a power loss only what
// was made durable, which is how a missing Sync or SyncDir shows up as lost data.
//
// It is safe for concurrent use. The zero value is not usable; use NewMemFS.
type MemFS struct {
	mu    sync.Mutex
	root  *dirNode
	steps int
	fault *Fault
	dead  bool
	// locks holds the files with a hold on them. A hold is on the file and not on its name, and a
	// restart finds none, as the operating system's.
	locks   map[*fileNode]bool
	noLocks bool
}

var _ FS = (*MemFS)(nil)

type fileNode struct {
	data    []byte
	durable []byte
}

type dirNode struct {
	entries map[string]any // *fileNode or *dirNode
	durable map[string]any
}

func newDir() *dirNode { return &dirNode{entries: map[string]any{}, durable: map[string]any{}} }

// NewMemFS returns an empty filesystem whose root exists and is durable.
func NewMemFS() *MemFS { return &MemFS{root: newDir(), locks: map[*fileNode]bool{}} }

// SetFault arms a fault. It replaces one not yet fired.
func (m *MemFS) SetFault(f Fault) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.fault = &f
}

// Steps is the number of write steps taken so far, which is also the index of the next one.
func (m *MemFS) Steps() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.steps
}

// CrashImage is the filesystem a restart finds after the process died now. With power set, only
// what was made durable is there; without it, everything that was written is. The result has no
// fault armed and is not crashed, and the receiver is unchanged.
func (m *MemFS) CrashImage(power bool) *MemFS {
	m.mu.Lock()
	defer m.mu.Unlock()
	return &MemFS{root: m.root.image(power), locks: map[*fileNode]bool{}}
}

// WithoutLocks makes every later Lock fail with ErrNoLocks, as OS.Lock does on a filesystem where
// a lock keeps nobody out.
func (m *MemFS) WithoutLocks() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.noLocks = true
}

// ForgetLocks drops every hold without telling its holders, as an NFS server that lost them does
// while the client that held one is cut off.
func (m *MemFS) ForgetLocks() {
	m.mu.Lock()
	defer m.mu.Unlock()
	clear(m.locks)
}

func (d *dirNode) image(power bool) *dirNode {
	source := d.entries
	if power {
		source = d.durable
	}
	out := newDir()
	for name, node := range source {
		switch n := node.(type) {
		case *fileNode:
			data := n.data
			if power {
				data = n.durable
			}
			out.entries[name] = &fileNode{data: slices.Clone(data), durable: slices.Clone(data)}
		case *dirNode:
			out.entries[name] = n.image(power)
		}
	}
	out.durable = maps.Clone(out.entries)
	return out
}

// Files returns the content of every file, by slash-separated path from the root.
func (m *MemFS) Files() map[string][]byte {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := map[string][]byte{}
	var walk func(prefix string, d *dirNode)
	walk = func(prefix string, d *dirNode) {
		for name, node := range d.entries {
			switch n := node.(type) {
			case *fileNode:
				out[prefix+name] = slices.Clone(n.data)
			case *dirNode:
				walk(prefix+name+"/", n)
			}
		}
	}
	walk("", m.root)
	return out
}

// Put writes a file, and the directories above it, durably and outside the step count. It is how
// a test makes a store directory a program did not write.
func (m *MemFS) Put(name string, data []byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	parts := split(name)
	d := m.root
	for _, part := range parts[:len(parts)-1] {
		next, ok := d.entries[part].(*dirNode)
		if !ok {
			next = newDir()
			d.entries[part], d.durable[part] = next, next
		}
		d = next
	}
	file := &fileNode{data: slices.Clone(data), durable: slices.Clone(data)}
	d.entries[parts[len(parts)-1]], d.durable[parts[len(parts)-1]] = file, file
}

func split(name string) []string {
	return strings.Split(strings.Trim(path.Clean("/"+name), "/"), "/")
}

func pathErr(op, name string, err error) error { return &fs.PathError{Op: op, Path: name, Err: err} }

// lookup finds the directory that holds name, and the last element of name. The caller holds mu.
func (m *MemFS) lookup(op, name string) (*dirNode, string, error) {
	if m.dead {
		return nil, "", pathErr(op, name, ErrCrashed)
	}
	parts := split(name)
	d := m.root
	for _, part := range parts[:len(parts)-1] {
		node, exists := d.entries[part]
		next, ok := node.(*dirNode)
		switch {
		case !exists:
			return nil, "", pathErr(op, name, fs.ErrNotExist)
		case !ok:
			return nil, "", pathErr(op, name, errNotDir)
		}
		d = next
	}
	return d, parts[len(parts)-1], nil
}

var (
	errNotDir = errors.New("not a directory")
	errIsDir  = errors.New("is a directory")
	// errNotEmpty matches fs.ErrExist, as the operating system's "directory not empty" does.
	errNotEmpty = fmt.Errorf("directory not empty: %w", fs.ErrExist)
)

// step counts one write step and says what it does: whether its effect happens, and the error to
// return after. torn is set when the effect is half a Write and the process then dies.
func (m *MemFS) step(op, name string) (effect, torn bool, err error) {
	if m.dead {
		return false, false, pathErr(op, name, ErrCrashed)
	}
	index := m.steps
	m.steps++
	if m.fault == nil || m.fault.At != index {
		return true, false, nil
	}
	kind := m.fault.Kind
	m.fault = nil
	switch kind {
	case FaultTornWrite:
		if op == "write" {
			m.dead = true
			return true, true, pathErr(op, name, ErrCrashed)
		}
		fallthrough
	case FaultCrash:
		m.dead = true
		return false, false, pathErr(op, name, ErrCrashed)
	case FaultFail:
		return false, false, pathErr(op, name, ErrInjected)
	case FaultFailAfter:
		return true, false, pathErr(op, name, ErrInjected)
	}
	return true, false, nil
}

// ReadFile implements FS.
func (m *MemFS) ReadFile(name string) ([]byte, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	d, base, err := m.lookup("read", name)
	if err != nil {
		return nil, err
	}
	node, exists := d.entries[base]
	file, ok := node.(*fileNode)
	switch {
	case !exists:
		return nil, pathErr("read", name, fs.ErrNotExist)
	case !ok:
		return nil, pathErr("read", name, errIsDir)
	}
	return slices.Clone(file.data), nil
}

// ReadDir implements FS.
func (m *MemFS) ReadDir(name string) ([]Entry, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	dir := m.root
	if strings.Trim(path.Clean("/"+name), "/") != "" {
		parent, base, err := m.lookup("readdir", name)
		if err != nil {
			return nil, err
		}
		node, exists := parent.entries[base]
		var ok bool
		if dir, ok = node.(*dirNode); !ok {
			if exists {
				return nil, pathErr("readdir", name, errNotDir)
			}
			return nil, pathErr("readdir", name, fs.ErrNotExist)
		}
	} else if m.dead {
		return nil, pathErr("readdir", name, ErrCrashed)
	}
	out := make([]Entry, 0, len(dir.entries))
	for entry, node := range dir.entries {
		switch n := node.(type) {
		case *fileNode:
			out = append(out, Entry{Name: entry, Size: int64(len(n.data))})
		case *dirNode:
			out = append(out, Entry{Name: entry, IsDir: true})
		}
	}
	slices.SortFunc(out, func(a, b Entry) int { return strings.Compare(a.Name, b.Name) })
	return out, nil
}

// Mkdir implements FS.
func (m *MemFS) Mkdir(name string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	d, base, err := m.lookup("mkdir", name)
	if err != nil {
		return err
	}
	effect, _, err := m.step("mkdir", name)
	if !effect {
		return err
	}
	if _, taken := d.entries[base]; taken {
		return pathErr("mkdir", name, fs.ErrExist)
	}
	d.entries[base] = newDir()
	return err
}

// Create implements FS.
func (m *MemFS) Create(name string) (File, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	d, base, err := m.lookup("create", name)
	if err != nil {
		return nil, err
	}
	effect, _, err := m.step("create", name)
	if !effect {
		return nil, err
	}
	file, ok := d.entries[base].(*fileNode)
	switch {
	case ok:
		file.data = nil
	case d.entries[base] != nil:
		return nil, pathErr("create", name, errIsDir)
	default:
		file = &fileNode{}
		d.entries[base] = file
	}
	return &memFile{fs: m, name: name, node: file}, err
}

// Rename implements FS.
func (m *MemFS) Rename(oldname, newname string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	from, fromBase, err := m.lookup("rename", oldname)
	if err != nil {
		return err
	}
	to, toBase, err := m.lookup("rename", newname)
	if err != nil {
		return err
	}
	file, ok := from.entries[fromBase].(*fileNode)
	if !ok {
		return pathErr("rename", oldname, fs.ErrNotExist)
	}
	if _, isDir := to.entries[toBase].(*dirNode); isDir {
		return pathErr("rename", newname, errIsDir)
	}
	effect, _, err := m.step("rename", oldname)
	if !effect {
		return err
	}
	delete(from.entries, fromBase)
	to.entries[toBase] = file
	return err
}

// Remove implements FS.
func (m *MemFS) Remove(name string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	d, base, err := m.lookup("remove", name)
	if err != nil {
		return err
	}
	node, ok := d.entries[base]
	if !ok {
		return pathErr("remove", name, fs.ErrNotExist)
	}
	if sub, isDir := node.(*dirNode); isDir && len(sub.entries) > 0 {
		return pathErr("remove", name, errNotEmpty)
	}
	effect, _, err := m.step("remove", name)
	if !effect {
		return err
	}
	delete(d.entries, base)
	return err
}

// SyncDir implements FS.
func (m *MemFS) SyncDir(name string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	dir := m.root
	if strings.Trim(path.Clean("/"+name), "/") != "" {
		parent, base, err := m.lookup("syncdir", name)
		if err != nil {
			return err
		}
		node, exists := parent.entries[base]
		var ok bool
		if dir, ok = node.(*dirNode); !ok {
			if exists {
				return pathErr("syncdir", name, errNotDir)
			}
			return pathErr("syncdir", name, fs.ErrNotExist)
		}
	}
	effect, _, err := m.step("syncdir", name)
	if !effect {
		return err
	}
	dir.durable = maps.Clone(dir.entries)
	return err
}

// Lock implements FS.
func (m *MemFS) Lock(name string) (Lock, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	d, base, err := m.lookup("lock", name)
	if err != nil {
		return nil, err
	}
	effect, _, err := m.step("lock", name)
	if !effect {
		return nil, err
	}
	file, ok := d.entries[base].(*fileNode)
	switch {
	case ok:
	case d.entries[base] != nil:
		return nil, pathErr("lock", name, errIsDir)
	default:
		file = &fileNode{}
		d.entries[base] = file
	}
	switch {
	case err != nil:
		return nil, err
	case m.noLocks:
		return nil, pathErr("lock", name, ErrNoLocks)
	case m.locks[file]:
		return nil, pathErr("lock", name, ErrLocked)
	}
	m.locks[file] = true
	return &memLock{fs: m, node: file}, nil
}

type memLock struct {
	fs   *MemFS
	node *fileNode
	done bool
}

// Release implements Lock.
func (l *memLock) Release() error {
	l.fs.mu.Lock()
	defer l.fs.mu.Unlock()
	if !l.done {
		l.done = true
		delete(l.fs.locks, l.node)
	}
	return nil
}

type memFile struct {
	fs   *MemFS
	name string
	node *fileNode
}

// Write implements File.
func (f *memFile) Write(p []byte) (int, error) {
	f.fs.mu.Lock()
	defer f.fs.mu.Unlock()
	effect, torn, err := f.fs.step("write", f.name)
	if !effect {
		return 0, err
	}
	if torn {
		half := len(p) / 2
		f.node.data = append(f.node.data, p[:half]...)
		return half, err
	}
	f.node.data = append(f.node.data, p...)
	return len(p), err
}

// Sync implements File.
func (f *memFile) Sync() error {
	f.fs.mu.Lock()
	defer f.fs.mu.Unlock()
	effect, _, err := f.fs.step("sync", f.name)
	if !effect {
		return err
	}
	f.node.durable = slices.Clone(f.node.data)
	return err
}

// Close implements File.
func (f *memFile) Close() error { return nil }
