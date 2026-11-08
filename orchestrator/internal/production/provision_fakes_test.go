package production

import (
	"context"
	"crypto/sha1"
	"errors"
	"fmt"
	"io/fs"
	"math/rand/v2"
	"os"
	"path"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"sync"
	"testing"
	"testing/fstest"
)

func readTestFile(t testing.TB, p string) string {
	t.Helper()
	data, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// errInjected is the failure a faults value makes.
var errInjected = errors.New("injected failure")

// faults numbers every step a Provisioner takes, in the file system and in git, and can break the
// one numbered failAt. With crash set, that step and every later one fails until heal, which is a host
// that died there: whatever it had made stays.
type faults struct {
	mu     sync.Mutex
	steps  int
	failAt int // -1 never
	crash  bool
	dead   bool
	trace  []string
}

func newFaults() *faults { return &faults{failAt: -1} }

func (f *faults) step(what string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := f.steps
	f.steps++
	f.trace = append(f.trace, what)
	if f.dead || n == f.failAt {
		f.dead = f.dead || f.crash
		return fmt.Errorf("%s (step %d): %w", what, n, errInjected)
	}
	return nil
}

// heal makes the host live again, with nothing broken.
func (f *faults) heal() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.dead, f.failAt = false, -1
}

type memNode struct {
	dir  bool
	data []byte
	perm fs.FileMode
}

// memFS is a ProvisionFS held in memory. Every method is a numbered step of its faults.
type memFS struct {
	mu     sync.Mutex
	nodes  map[string]memNode
	faults *faults
	temps  int
	// beforeRename runs just before Rename, as another process would act then.
	beforeRename func(m *memFS)
}

var _ ProvisionFS = (*memFS)(nil)

func newMemFS(f *faults) *memFS {
	return &memFS{nodes: map[string]memNode{"/": {dir: true}}, faults: f}
}

func (m *memFS) step(what, p string) error { return m.faults.step(what + " " + p) }

func (m *memFS) MkdirAll(dir string) error {
	if err := m.step("mkdirall", dir); err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.mkdirAll(path.Clean(dir))
}

func (m *memFS) mkdirAll(dir string) error {
	if dir == "/" {
		return nil
	}
	if n, ok := m.nodes[dir]; ok {
		if !n.dir {
			return fmt.Errorf("%s: not a directory", dir)
		}
		return nil
	}
	if err := m.mkdirAll(path.Dir(dir)); err != nil {
		return err
	}
	m.nodes[dir] = memNode{dir: true}
	return nil
}

func (m *memFS) RealPath(p string) (string, error) {
	if err := m.step("realpath", p); err != nil {
		return "", err
	}
	return path.Clean(p), nil
}

func (m *memFS) Exists(p string) (bool, error) {
	if err := m.step("exists", p); err != nil {
		return false, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	_, ok := m.nodes[path.Clean(p)]
	return ok, nil
}

func (m *memFS) ReadFile(p string) ([]byte, error) {
	if err := m.step("readfile", p); err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	n, ok := m.nodes[path.Clean(p)]
	if !ok || n.dir {
		return nil, fmt.Errorf("%s: %w", p, fs.ErrNotExist)
	}
	return slices.Clone(n.data), nil
}

func (m *memFS) WriteFile(p string, data []byte, perm fs.FileMode) error {
	if err := m.step("writefile", p); err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.put(path.Clean(p), memNode{data: slices.Clone(data), perm: perm})
}

func (m *memFS) put(p string, n memNode) error {
	if parent, ok := m.nodes[path.Dir(p)]; !ok || !parent.dir {
		return fmt.Errorf("%s: no parent directory", p)
	}
	if old, ok := m.nodes[p]; ok && old.dir {
		return fmt.Errorf("%s: is a directory", p)
	}
	m.nodes[p] = n
	return nil
}

func (m *memFS) Mkdir(dir string) error {
	if err := m.step("mkdir", dir); err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	dir = path.Clean(dir)
	if _, ok := m.nodes[dir]; ok {
		return fmt.Errorf("%s: %w", dir, fs.ErrExist)
	}
	return m.put(dir, memNode{dir: true})
}

func (m *memFS) MkdirTemp(parent, prefix string) (string, error) {
	if err := m.step("mkdirtemp", path.Join(parent, prefix)); err != nil {
		return "", err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.temps++
	dir := path.Join(parent, fmt.Sprintf("%s%04d", prefix, m.temps))
	return dir, m.put(dir, memNode{dir: true})
}

func (m *memFS) ReadDirNames(dir string) ([]string, error) {
	if err := m.step("readdir", dir); err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.children(path.Clean(dir)), nil
}

func (m *memFS) children(dir string) []string {
	var names []string
	for p := range m.nodes {
		if p != "/" && path.Dir(p) == dir {
			names = append(names, path.Base(p))
		}
	}
	sort.Strings(names)
	return names
}

func (m *memFS) Rename(from, to string) error {
	if err := m.step("rename", from+" -> "+to); err != nil {
		return err
	}
	if m.beforeRename != nil {
		m.beforeRename(m)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	from, to = path.Clean(from), path.Clean(to)
	if _, ok := m.nodes[from]; !ok {
		return fmt.Errorf("%s: %w", from, fs.ErrNotExist)
	}
	if old, ok := m.nodes[to]; ok && (!old.dir || len(m.children(to)) > 0) {
		return fmt.Errorf("%s: %w", to, fs.ErrExist)
	}
	moved := map[string]memNode{}
	for p, n := range m.nodes {
		if p == from || strings.HasPrefix(p, from+"/") {
			moved[to+strings.TrimPrefix(p, from)] = n
			delete(m.nodes, p)
		}
	}
	for p, n := range moved {
		m.nodes[p] = n
	}
	return nil
}

func (m *memFS) RemoveAll(p string) error {
	if err := m.step("removeall", p); err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	p = path.Clean(p)
	for q := range m.nodes {
		if q == p || strings.HasPrefix(q, p+"/") {
			delete(m.nodes, q)
		}
	}
	return nil
}

// files is the tree under dir as relative path to content (with the mode), directories as "dir", the
// way snapshot reads a source, so the two compare. Names under .git are kept only when withGit.
func (m *memFS) files(dir string, withGit bool) map[string]string {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := map[string]string{}
	for p, n := range m.nodes {
		rel, ok := strings.CutPrefix(p, dir+"/")
		if !ok || (!withGit && (rel == ".git" || strings.HasPrefix(rel, ".git/"))) {
			continue
		}
		out[rel] = describe(n)
	}
	return out
}

func describe(n memNode) string {
	if n.dir {
		return "dir"
	}
	return fmt.Sprintf("%o:%s", n.perm&0o777, n.data)
}

// snapshot is a source tree as files reads a made one.
func snapshot(src fs.FS) map[string]string {
	out := map[string]string{}
	_ = fs.WalkDir(src, ".", func(name string, d fs.DirEntry, err error) error {
		if err != nil || name == "." {
			return err
		}
		if d.IsDir() {
			out[name] = "dir"
			return nil
		}
		info, _ := d.Info()
		data, _ := fs.ReadFile(src, name)
		out[name] = describe(memNode{data: data, perm: info.Mode()})
		return nil
	})
	return out
}

// commitOf is what the Fake git calls the commit of a tree: it depends on the files and nothing else.
func commitOf(tree map[string]string) string {
	keys := make([]string, 0, len(tree))
	for k := range tree {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := sha1.New()
	for _, k := range keys {
		fmt.Fprintf(h, "%s\x00%s\x00", k, tree[k])
	}
	return fmt.Sprintf("%x", h.Sum(nil))
}

// fakeGit is a GitRunner over a memFS: it makes the .git directory and the commit a real git would,
// without a process, and clones from remotes a test sets (url, then revision, to files).
type fakeGit struct {
	fs      *memFS
	remotes map[string]map[string]map[string]string
}

var _ GitRunner = (*fakeGit)(nil)

func (g *fakeGit) Run(_ context.Context, dir string, args ...string) (string, error) {
	if err := g.fs.faults.step("git " + strings.Join(args, " ")); err != nil {
		return "", err
	}
	dir = path.Clean(dir)
	exists := func(p string) bool {
		g.fs.mu.Lock()
		defer g.fs.mu.Unlock()
		_, ok := g.fs.nodes[p]
		return ok
	}
	if !exists(dir) {
		return "", fmt.Errorf("git: %s: no such directory", dir)
	}
	put := func(p string, n memNode) error {
		g.fs.mu.Lock()
		defer g.fs.mu.Unlock()
		return g.fs.put(p, n)
	}
	switch args[0] {
	case "init":
		if err := put(dir+"/.git", memNode{dir: true}); err != nil {
			return "", err
		}
		return "", nil
	case "add":
		return "", nil
	case "commit":
		return "", put(dir+"/.git/COMMIT", memNode{data: []byte(commitOf(g.fs.files(dir, false)))})
	case "rev-parse":
		g.fs.mu.Lock()
		defer g.fs.mu.Unlock()
		n, ok := g.fs.nodes[dir+"/.git/COMMIT"]
		if !ok {
			return "", errors.New("git: no commit")
		}
		return string(n.data) + "\n", nil
	case "clone":
		url := args[len(args)-2]
		if _, ok := g.remotes[url]; !ok {
			return "", fmt.Errorf("git: cannot reach %s", url)
		}
		return "", put(dir+"/.git", memNode{dir: true, data: []byte(url)})
	case "checkout":
		g.fs.mu.Lock()
		defer g.fs.mu.Unlock()
		url := string(g.fs.nodes[dir+"/.git"].data)
		files, ok := g.remotes[url][args[2]]
		if !ok {
			return "", fmt.Errorf("git: no revision %s", args[2])
		}
		for rel, content := range files {
			for d := path.Dir(rel); d != "."; d = path.Dir(d) {
				g.fs.nodes[dir+"/"+d] = memNode{dir: true}
			}
			g.fs.nodes[dir+"/"+rel] = memNode{data: []byte(content), perm: 0o644}
		}
		return "", nil
	}
	return "", fmt.Errorf("git %s: not faked", args[0])
}

// randomTree is a source tree of nested directories (some empty), files of random bytes (some empty)
// and both file modes. Names never repeat in a directory and are never .git.
func randomTree(r *rand.Rand) fstest.MapFS {
	tree := fstest.MapFS{}
	var fill func(dir string, depth int)
	fill = func(dir string, depth int) {
		for i, n := 0, r.IntN(5); i < n; i++ {
			name := fmt.Sprintf("%c%d", 'a'+rune(r.IntN(4)), i)
			p := path.Join(dir, name)
			if depth < 3 && r.IntN(3) == 0 {
				tree[p] = &fstest.MapFile{Mode: fs.ModeDir | 0o755}
				fill(p, depth+1)
				continue
			}
			data := make([]byte, r.IntN(40))
			for j := range data {
				data[j] = byte(r.IntN(256))
			}
			mode := fs.FileMode(0o644)
			if r.IntN(4) == 0 {
				mode = 0o755
			}
			tree[p] = &fstest.MapFile{Data: data, Mode: mode}
		}
	}
	fill(".", 0)
	return tree
}

// randomName is a valid repository name.
func randomName(r *rand.Rand) string {
	const first, rest = "abcxyz019", "abcxyz019-"
	b := []byte{first[r.IntN(len(first))]}
	for i, n := 0, r.IntN(20); i < n; i++ {
		b = append(b, rest[r.IntN(len(rest))])
	}
	return string(b)
}

func repoPath(parent, name string) string { return filepath.Join(parent, name) }
