package task

import (
	"io"
	"io/fs"
	"path"
	"sort"
	"strings"
	"time"
)

// File is a file of a Tree.
type File struct {
	Data       []byte
	Executable bool
}

// Tree is the files of a repository at one commit, in memory: slash-separated paths from the
// repository root to their content. It is an fs.FS, so Read and Version take it as they take a
// directory, and it is what a source that cannot hand out a directory, such as git, makes.
type Tree map[string]File

var _ fs.FS = Tree(nil)

type treeEntry struct {
	name string
	file *File // nil for a directory
}

func (e treeEntry) Name() string { return e.name }
func (e treeEntry) IsDir() bool  { return e.file == nil }
func (e treeEntry) Type() fs.FileMode {
	if e.file == nil {
		return fs.ModeDir
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
	if f, ok := t[name]; ok {
		f := f
		return &openFile{treeEntry: treeEntry{name: path.Base(name), file: &f}, r: strings.NewReader(string(f.Data))}, nil
	}
	prefix := name + "/"
	if name == "." {
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
	if len(children) == 0 && name != "." {
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
