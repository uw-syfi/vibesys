// Package releasetest lays out an installed release for tests: in memory, to hand to code that
// reads a release through an fs.FS, or on disk, for code that reads files itself.
package releasetest

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// Option changes the release a test lays out.
type Option func(*spec)

type spec struct {
	version, goos, goarch string
	without               map[string]bool
	content               map[string]string
	examples              []Example
}

// Example is an example task a test puts in the release: its id, the task's name, the files of its
// folder (name to text) and, for one cloned from elsewhere, where from.
type Example struct {
	ID    string
	Task  string
	Files map[string]string
	Clone *release.CloneSpec
}

// WithExamples puts example tasks in the release: their folders under examples/ and a catalogue
// that lists them.
func WithExamples(examples ...Example) Option {
	return func(s *spec) { s.examples = append(s.examples, examples...) }
}

// Without leaves out files, named relative to the release.
func Without(files ...string) Option {
	return func(s *spec) {
		for _, f := range files {
			s.without[f] = true
		}
	}
}

// Platform builds the release for another operating system and architecture.
func Platform(goos, goarch string) Option {
	return func(s *spec) { s.goos, s.goarch = goos, goarch }
}

// Version gives the release another version.
func Version(version string) Option { return func(s *spec) { s.version = version } }

// Content sets what a file holds; every other file holds "x".
func Content(file, text string) Option {
	return func(s *spec) { s.content[file] = text }
}

// Tree is the installed release at dir (absolute) as an fs.FS rooted at "/".
func Tree(t testing.TB, dir string, options ...Option) fstest.MapFS {
	t.Helper()
	s := &spec{version: "1.0.0", goos: "linux", goarch: "amd64", without: map[string]bool{}, content: map[string]string{}}
	for _, o := range options {
		o(s)
	}
	m, err := release.NewManifest(s.version, s.goos, s.goarch)
	if err != nil {
		t.Fatal(err)
	}
	data, err := release.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	root := strings.TrimPrefix(filepath.ToSlash(dir), "/") + "/"
	tree := fstest.MapFS{root + release.ManifestName: {Data: data, Mode: 0o644}}
	text := func(f string) []byte {
		if c, ok := s.content[f]; ok {
			return []byte(c)
		}
		return []byte("x")
	}
	for _, f := range m.GetRequiredFiles() {
		tree[root+f] = &fstest.MapFile{Data: text(f), Mode: 0o644}
	}
	catalogue := &pb.ReleaseCatalogue{Release: s.version}
	for _, e := range s.examples {
		re := &pb.ReleaseExample{
			Example: e.ID, Folder: e.ID[strings.LastIndex(e.ID, "/")+1:],
			Tree: release.ExamplesRoot + "/" + e.ID, Card: &cliv1.TaskCard{Task: e.Task},
		}
		if e.Clone != nil {
			re.NeedsClone = true
			re.Clone = &pb.CloneSource{Url: e.Clone.URL, Revision: e.Clone.Revision}
		}
		catalogue.Examples = append(catalogue.Examples, re)
		tree[root+re.Tree] = &fstest.MapFile{Mode: fs.ModeDir | 0o755}
		for name, text := range e.Files {
			tree[root+re.Tree+"/"+name] = &fstest.MapFile{Data: []byte(text), Mode: 0o644}
		}
	}
	if data, err := release.MarshalCatalogue(catalogue); err != nil {
		t.Fatal(err)
	} else if _, set := s.content[release.CatalogueName]; !set {
		tree[root+release.CatalogueName] = &fstest.MapFile{Data: data, Mode: 0o644}
	}
	for _, f := range []string{m.GetHostProgram(), m.GetJobTool(), m.GetSessionHolderCommand()[0]} {
		tree[root+f] = &fstest.MapFile{Data: text(f), Mode: 0o755}
	}
	for f := range s.without {
		delete(tree, root+f)
	}
	return tree
}

// Write lays the same release out on disk under dir, which must exist.
func Write(t testing.TB, dir string, options ...Option) {
	t.Helper()
	for name, file := range Tree(t, dir, options...) {
		path := filepath.Join("/", name)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, file.Data, file.Mode&fs.ModePerm); err != nil {
			t.Fatal(err)
		}
	}
}
