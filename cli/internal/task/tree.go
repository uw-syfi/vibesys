package task

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"path"
	"sort"
	"strconv"
	"strings"
	"time"
)

// File is a file of a Tree: a regular file, or a symbolic link when Link is set.
type File struct {
	Data       []byte
	Executable bool
	// Link is the target of a symbolic link, as the link holds it. A link has no Data and is not
	// Executable.
	Link string
}

// IsLink says whether the file is a symbolic link.
func (f File) IsLink() bool { return f.Link != "" }

// Tree is the files of a repository at one commit, in memory: slash-separated paths from the
// repository root to their content. It is an fs.FS, so Read and Version take it as they take a
// directory, and it is what a source that cannot hand out a directory, such as git, makes. Its
// symbolic links are entries of their own: Open follows them, as a directory does, and ReadLink
// and Lstat see the link itself. A link is followed only inside the tree.
type Tree map[string]File

var (
	_ fs.FS         = Tree(nil)
	_ fs.ReadLinkFS = Tree(nil)
)

// maxLinkHops bounds how many links one path may pass through, as an operating system bounds it,
// so a cycle of links is an error and not a hang.
const maxLinkHops = 40

// linkFault is why a symbolic link of a tree cannot be followed inside it.
type linkFault int

const (
	// linkAbsolute is a link to an absolute path, which names a place outside any tree.
	linkAbsolute linkFault = iota + 1
	// linkEscapes is a relative link whose target is above the tree's root.
	linkEscapes
	// linkDangles is a link whose target the tree does not have.
	linkDangles
	// linkLoops is a link on a path through more than maxLinkHops links, as a cycle makes.
	linkLoops
)

// linkError says which symbolic link of a tree cannot be followed inside it, and why.
type linkError struct {
	// Path is the link, by its path from the tree's root.
	Path string
	// Target is what the link holds.
	Target string
	Fault  linkFault
	// Missing is, for linkDangles, the path from the tree's root the link leads to.
	Missing string
}

func (e *linkError) Error() string {
	return fmt.Sprintf("%s is a symbolic link to %q, which %s", e.Path, e.Target, e.why("the tree"))
}

// why says what is wrong with the link, naming the tree as where.
func (e *linkError) why(where string) string {
	switch e.Fault {
	case linkAbsolute:
		return "is an absolute path and so outside " + where
	case linkEscapes:
		return "points outside " + where
	case linkDangles:
		return "points at nothing: " + where + " has no " + e.Missing
	case linkLoops:
		return "passes through more than " + strconv.Itoa(maxLinkHops) + " links, as a cycle of links does"
	}
	return "cannot be followed"
}

// resolve returns the path of the file or directory that name stands for once every link on the
// way is followed, by its path from the tree's root. It fails with a *linkError for a link that
// is absolute, that leaves the tree, that points at nothing the tree has, or that is part of a
// cycle. It does not check that the last element exists unless it is reached through a link.
func (t Tree) resolve(name string) (string, error) {
	hops := 0
	return t.resolveFrom(name, &hops)
}

func (t Tree) resolveFrom(name string, hops *int) (string, error) {
	if name == "." {
		return ".", nil
	}
	cur := "."
	for _, elem := range strings.Split(name, "/") {
		next := path.Join(cur, elem)
		f, ok := t[next]
		if !ok || !f.IsLink() {
			cur = next
			continue
		}
		if *hops++; *hops > maxLinkHops {
			return "", &linkError{Path: next, Target: f.Link, Fault: linkLoops}
		}
		if path.IsAbs(f.Link) {
			return "", &linkError{Path: next, Target: f.Link, Fault: linkAbsolute}
		}
		target := path.Join(cur, f.Link)
		if target == ".." || strings.HasPrefix(target, "../") {
			return "", &linkError{Path: next, Target: f.Link, Fault: linkEscapes}
		}
		resolved, err := t.resolveFrom(target, hops)
		if err != nil {
			return "", err
		}
		if !t.has(resolved) {
			return "", &linkError{Path: next, Target: f.Link, Fault: linkDangles, Missing: resolved}
		}
		cur = resolved
	}
	return cur, nil
}

// has says whether the tree has a file, a link or a directory at name.
func (t Tree) has(name string) bool {
	if name == "." {
		return true
	}
	if _, ok := t[name]; ok {
		return true
	}
	for p := range t {
		if strings.HasPrefix(p, name+"/") {
			return true
		}
	}
	return false
}

// cutLast splits a path at its last slash, and says whether it had one.
func cutLast(name string) (dir, base string, nested bool) {
	i := strings.LastIndexByte(name, '/')
	if i < 0 {
		return "", name, false
	}
	return name[:i], name[i+1:], true
}

// ReadLink implements fs.ReadLinkFS.
func (t Tree) ReadLink(name string) (string, error) {
	f, err := t.lstat(name)
	if err != nil {
		return "", err
	}
	if !f.IsLink() {
		return "", &fs.PathError{Op: "readlink", Path: name, Err: fs.ErrInvalid}
	}
	return f.Link, nil
}

// Lstat implements fs.ReadLinkFS.
func (t Tree) Lstat(name string) (fs.FileInfo, error) {
	f, err := t.lstat(name)
	if errors.Is(err, fs.ErrNotExist) && t.has(name) {
		return treeEntry{name: path.Base(name)}, nil
	}
	if err != nil {
		return nil, err
	}
	return treeEntry{name: path.Base(name), file: &f}, nil
}

// lstat returns the entry at name without following a link at its last element.
func (t Tree) lstat(name string) (File, error) {
	if !fs.ValidPath(name) {
		return File{}, &fs.PathError{Op: "lstat", Path: name, Err: fs.ErrInvalid}
	}
	parent, elem := ".", name
	if dir, base, nested := cutLast(name); nested {
		resolved, err := t.resolve(dir)
		if err != nil {
			return File{}, &fs.PathError{Op: "lstat", Path: name, Err: err}
		}
		parent, elem = resolved, base
	}
	f, ok := t[path.Join(parent, elem)]
	if !ok {
		return File{}, &fs.PathError{Op: "lstat", Path: name, Err: fs.ErrNotExist}
	}
	return f, nil
}

type treeEntry struct {
	name string
	file *File // nil for a directory
}

func (e treeEntry) Name() string { return e.name }
func (e treeEntry) IsDir() bool  { return e.file == nil }
func (e treeEntry) Type() fs.FileMode {
	switch {
	case e.file == nil:
		return fs.ModeDir
	case e.file.IsLink():
		return fs.ModeSymlink
	}
	return 0
}
func (e treeEntry) Info() (fs.FileInfo, error) { return e, nil }
func (e treeEntry) Size() int64 {
	if e.file == nil {
		return 0
	}
	return int64(len(e.file.Data))
}
func (e treeEntry) Mode() fs.FileMode {
	switch {
	case e.file == nil:
		return fs.ModeDir | 0o755
	case e.file.IsLink():
		return fs.ModeSymlink | 0o777
	case e.file.Executable:
		return 0o755
	}
	return 0o644
}
func (e treeEntry) ModTime() time.Time { return time.Time{} }
func (e treeEntry) Sys() any           { return nil }

// openFile is an open file of a Tree.
type openFile struct {
	treeEntry
	r *strings.Reader
}

func (f *openFile) Stat() (fs.FileInfo, error) { return f.treeEntry, nil }
func (f *openFile) Read(p []byte) (int, error) { return f.r.Read(p) }
func (f *openFile) Close() error               { return nil }

// openDir is an open directory of a Tree.
type openDir struct {
	treeEntry
	entries []fs.DirEntry
	pos     int
}

func (d *openDir) Stat() (fs.FileInfo, error) { return d.treeEntry, nil }
func (d *openDir) Read([]byte) (int, error) {
	return 0, &fs.PathError{Op: "read", Path: d.name, Err: fs.ErrInvalid}
}
func (d *openDir) Close() error { return nil }
func (d *openDir) ReadDir(n int) ([]fs.DirEntry, error) {
	rest := d.entries[d.pos:]
	if n <= 0 {
		d.pos = len(d.entries)
		return rest, nil
	}
	if len(rest) == 0 {
		return nil, io.EOF
	}
	if n > len(rest) {
		n = len(rest)
	}
	d.pos += n
	return rest[:n], nil
}

// Open implements fs.FS.
func (t Tree) Open(name string) (fs.File, error) {
	if !fs.ValidPath(name) {
		return nil, &fs.PathError{Op: "open", Path: name, Err: fs.ErrInvalid}
	}
	// Open follows links, as opening a path of a directory does.
	resolved, err := t.resolve(name)
	if err != nil {
		return nil, &fs.PathError{Op: "open", Path: name, Err: err}
	}
	if f, ok := t[resolved]; ok {
		f := f
		return &openFile{treeEntry: treeEntry{name: path.Base(name), file: &f}, r: strings.NewReader(string(f.Data))}, nil
	}
	prefix := resolved + "/"
	if resolved == "." {
		prefix = ""
	}
	children := map[string]treeEntry{}
	for p, f := range t {
		rest, ok := strings.CutPrefix(p, prefix)
		if !ok {
			continue
		}
		first, _, nested := strings.Cut(rest, "/")
		if nested {
			children[first] = treeEntry{name: first}
			continue
		}
		f := f
		children[first] = treeEntry{name: first, file: &f}
	}
	if len(children) == 0 && resolved != "." {
		return nil, &fs.PathError{Op: "open", Path: name, Err: fs.ErrNotExist}
	}
	names := make([]string, 0, len(children))
	for n := range children {
		names = append(names, n)
	}
	sort.Strings(names)
	d := &openDir{treeEntry: treeEntry{name: path.Base(name)}}
	for _, n := range names {
		d.entries = append(d.entries, children[n])
	}
	return d, nil
}
