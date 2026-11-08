package assemble_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"math/rand/v2"
	"os"
	"path"
	"slices"
	"sort"
	"strings"
	"testing"
	"testing/fstest"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/release"
	"github.com/uw-syfi/vibesys/orchestrator/release/assemble"
	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

const (
	stageDir = "/stage"
	rev      = "89abcdef0123456789abcdef0123456789abcdef"
)

// memSink is a Sink that keeps what is written in memory, as a file system.
type memSink struct{ files fstest.MapFS }

func newSink() *memSink { return &memSink{files: fstest.MapFS{}} }

func (s *memSink) MkdirAll(dir string) error {
	for d := dir; d != "." && d != "/"; d = path.Dir(d) {
		if f, ok := s.files[d]; ok && f.Mode&fs.ModeDir == 0 {
			return fmt.Errorf("%s is a file", d)
		}
		s.files[d] = &fstest.MapFile{Mode: fs.ModeDir | 0o755}
	}
	return nil
}

func (s *memSink) WriteFile(name string, data []byte, perm fs.FileMode) error {
	if err := s.MkdirAll(path.Dir(name)); err != nil {
		return err
	}
	s.files[name] = &fstest.MapFile{Data: data, Mode: perm}
	return nil
}

// fakeExternals serves the own files of external examples a test sets, by example path.
type fakeExternals struct {
	own  map[string]fs.FS
	pins map[string]assemble.Pin
	fail map[string]error
	asks []string
}

func (f *fakeExternals) Fetch(_ context.Context, e assemble.Entry) (fs.FS, assemble.Pin, error) {
	f.asks = append(f.asks, e.Path)
	if err := f.fail[e.Path]; err != nil {
		return nil, assemble.Pin{}, err
	}
	return f.own[e.Path], f.pins[e.Path], nil
}

// fakeCards answers every card with the task's name, and records where it was asked.
type fakeCards struct {
	fail    map[string]error // by "folder:task"
	renamed map[string]string
	asked   []string
}

func (f *fakeCards) Card(_ context.Context, folder, task string) (*cliv1.TaskCard, error) {
	key := folder + ":" + task
	f.asked = append(f.asked, key)
	if err := f.fail[key]; err != nil {
		return nil, err
	}
	if name, ok := f.renamed[key]; ok {
		task = name
	}
	return &cliv1.TaskCard{Task: task, Heading: "heading of " + task}, nil
}

func file(text string) *fstest.MapFile { return &fstest.MapFile{Data: []byte(text), Mode: 0o644} }

func entry(p string, tasks ...string) assemble.Entry { return assemble.Entry{Path: p, Tasks: tasks} }

func TestStageCarriesAnExampleAndListsItsTasks(t *testing.T) {
	repo := fstest.MapFS{
		"examples/db/kv/.vibesys/tasks/b/vibesys.input.toml": file("b"),
		"examples/db/kv/.vibesys/tasks/a/vibesys.input.toml": file("a"),
		"examples/db/kv/src/lib.rs":                          file("lib"),
		"examples/db/kv/run.sh":                              &fstest.MapFile{Data: []byte("#!/bin/sh"), Mode: 0o751},
		"examples/db/other/.vibesys/tasks/t/x":               file("x"),
	}
	sink := newSink()
	staged, err := assemble.Stage(t.Context(), repo, []assemble.Entry{entry("examples/db/kv")}, nil, sink)
	if err != nil {
		t.Fatal(err)
	}
	if len(staged) != 1 || staged[0].ID() != "db/kv" || !slices.Equal(staged[0].Tasks, []string{"a", "b"}) || staged[0].Pin != nil {
		t.Fatalf("staged = %+v", staged)
	}
	if got := string(sink.files["examples/db/kv/src/lib.rs"].Data); got != "lib" {
		t.Fatalf("lib.rs = %q", got)
	}
	if m := sink.files["examples/db/kv/run.sh"].Mode; m != 0o755 {
		t.Fatalf("an executable file is %v", m)
	}
	if _, there := sink.files["examples/db/other/.vibesys/tasks/t/x"]; there {
		t.Fatal("an example the registry does not list was carried")
	}
}

func TestStageNamesATaskTheRegistryListsAndTheFolderLacks(t *testing.T) {
	repo := fstest.MapFS{"examples/a/b/.vibesys/tasks/real/f": file("")}
	_, err := assemble.Stage(t.Context(), repo, []assemble.Entry{entry("examples/a/b", "real", "ghost")}, nil, newSink())
	if err == nil || !strings.Contains(err.Error(), "ghost") || !strings.Contains(err.Error(), "a/b") {
		t.Fatalf("err = %v", err)
	}
	_, err = assemble.Stage(t.Context(), fstest.MapFS{"examples/a/b/readme": file("")}, []assemble.Entry{entry("examples/a/b")}, nil, newSink())
	if err == nil {
		t.Fatal("an example with no task was carried")
	}
}

func TestStageCarriesOnlyTheOwnFilesOfAnExternalExampleAndPinsTheRest(t *testing.T) {
	// The checkout holds the submodule's files too, as on a machine that cloned it. Stage never asks
	// the checkout for them: it carries what Externals gives.
	repo := fstest.MapFS{"examples/ms/repositories/vllm/vllm/engine.py": file("secret source")}
	ext := &fakeExternals{
		own: map[string]fs.FS{"examples/ms/repositories/vllm": fstest.MapFS{
			".vibesys/tasks/default/vibesys.input.toml": file("t"),
			"vllm/engine.py": file("not carried"),
		}},
		pins: map[string]assemble.Pin{"examples/ms/repositories/vllm": {URL: "https://example.invalid/vllm.git", Revision: rev}},
	}
	sink := newSink()
	e := assemble.Entry{Path: "examples/ms/repositories/vllm", External: true}
	staged, err := assemble.Stage(t.Context(), repo, []assemble.Entry{e}, ext, sink)
	if err != nil {
		t.Fatal(err)
	}
	if s := staged[0]; s.Pin == nil || *s.Pin != (assemble.Pin{URL: "https://example.invalid/vllm.git", Revision: rev}) || !slices.Equal(s.Tasks, []string{"default"}) {
		t.Fatalf("staged = %+v", s)
	}
	for name := range sink.files {
		if strings.Contains(name, "engine.py") {
			t.Fatalf("carried %s of the external repository", name)
		}
	}
	if _, ok := sink.files["examples/ms/repositories/vllm/.vibesys/tasks/default/vibesys.input.toml"]; !ok {
		t.Fatal("the example's own task file is not carried")
	}
}

func TestStageRefusesAPinThatIsNotAFullCommit(t *testing.T) {
	for _, pin := range []assemble.Pin{{URL: "u", Revision: "main"}, {URL: "u", Revision: rev[:39]}, {URL: "", Revision: rev}, {URL: "--upload-pack=x", Revision: rev}, {URL: "u", Revision: strings.ToUpper(rev)}} {
		ext := &fakeExternals{
			own:  map[string]fs.FS{"examples/a/repositories/x": fstest.MapFS{".vibesys/tasks/t/f": file("")}},
			pins: map[string]assemble.Pin{"examples/a/repositories/x": pin},
		}
		_, err := assemble.Stage(t.Context(), fstest.MapFS{}, []assemble.Entry{{Path: "examples/a/repositories/x", External: true}}, ext, newSink())
		if err == nil {
			t.Errorf("pin %+v accepted", pin)
		}
	}
	ext := &fakeExternals{fail: map[string]error{"examples/a/repositories/x": errors.New("network down")}}
	_, err := assemble.Stage(t.Context(), fstest.MapFS{}, []assemble.Entry{{Path: "examples/a/repositories/x", External: true}}, ext, newSink())
	if err == nil || !strings.Contains(err.Error(), "network down") || !strings.Contains(err.Error(), "a/repositories/x") {
		t.Fatalf("err = %v", err)
	}
}

func TestStageLeavesOutNestedRepositoriesAndRefusesLinks(t *testing.T) {
	repo := fstest.MapFS{
		"examples/a/b/.vibesys/tasks/t/f":                   file(""),
		"examples/a/b/.vibesys/tasks/t/reference/Show/.git": file("gitdir: ../../../../../../.git/modules/x"),
		"examples/a/b/.vibesys/tasks/t/reference/Show/x.py": file("another repository's"),
		"examples/a/b/vendor/clone/.git/HEAD":               file("ref"),
		"examples/a/b/vendor/clone/y.c":                     file("another repository's"),
		"examples/a/b/keep/__pycache__/m.pyc":               file("junk"),
		"examples/a/b/keep/.DS_Store":                       file("junk"),
		"examples/a/b/keep/k.txt":                           file("kept"),
	}
	sink := newSink()
	if _, err := assemble.Stage(t.Context(), repo, []assemble.Entry{entry("examples/a/b")}, nil, sink); err != nil {
		t.Fatal(err)
	}
	var names []string
	for n, f := range sink.files {
		if f.Mode&fs.ModeDir == 0 {
			names = append(names, n)
		}
	}
	sort.Strings(names)
	want := []string{"examples/a/b/.vibesys/tasks/t/f", "examples/a/b/keep/k.txt"}
	if !slices.Equal(names, want) {
		t.Fatalf("carried %v, want %v", names, want)
	}

	linked := fstest.MapFS{
		"examples/a/b/.vibesys/tasks/t/f": file(""),
		"examples/a/b/escape":             &fstest.MapFile{Data: []byte("/etc/passwd"), Mode: fs.ModeSymlink | 0o777},
	}
	if _, err := assemble.Stage(t.Context(), linked, []assemble.Entry{entry("examples/a/b")}, nil, newSink()); err == nil || !strings.Contains(err.Error(), "escape") {
		t.Fatalf("a link was carried or not named: %v", err)
	}
	rootRepo := fstest.MapFS{"examples/a/b/.vibesys/tasks/t/f": file(""), "examples/a/b/.git/HEAD": file("")}
	if _, err := assemble.Stage(t.Context(), rootRepo, []assemble.Entry{entry("examples/a/b")}, nil, newSink()); err == nil {
		t.Fatal("an example folder that is itself a repository was carried")
	}
}

// Whatever tree an example holds, what is carried is exactly its regular files that are not
// leftovers and not inside a nested repository, with their bytes; nothing else is, and no carried
// name leaves examples/ID.
func TestStageCarriesExactlyTheFilesTheRulesKeep(t *testing.T) {
	rng := rand.New(rand.NewPCG(1, 2))
	segments := []string{"src", "lib", "data", "__pycache__", "nested", "docs", ".vibesys"}
	for range 80 {
		repo := fstest.MapFS{"examples/f/e/.vibesys/tasks/t/vibesys.input.toml": file("t")}
		want := map[string]string{}
		nested := []string{}
		for range rng.IntN(12) {
			dir := "examples/f/e"
			for range 1 + rng.IntN(3) {
				dir = path.Join(dir, segments[rng.IntN(len(segments))])
			}
			if rng.IntN(5) == 0 {
				repo[path.Join(dir, ".git")] = file("gitdir: elsewhere")
				nested = append(nested, dir)
			}
			name := path.Join(dir, fmt.Sprintf("f%d", rng.IntN(100)))
			repo[name] = file(name)
		}
		for name, f := range repo {
			if path.Base(name) == ".git" {
				continue
			}
			skipped := strings.Contains(name+"/", "/__pycache__/")
			for _, n := range nested {
				skipped = skipped || strings.HasPrefix(name, n+"/")
			}
			if !skipped {
				want[name] = string(f.Data)
			}
		}
		sink := newSink()
		if _, err := assemble.Stage(t.Context(), repo, []assemble.Entry{entry("examples/f/e")}, nil, sink); err != nil {
			t.Fatal(err)
		}
		got := map[string]string{}
		for n, f := range sink.files {
			if f.Mode&fs.ModeDir != 0 {
				continue
			}
			got[n] = string(f.Data)
			if !strings.HasPrefix(n, "examples/f/e/") || strings.Contains(n, "..") {
				t.Fatalf("carried %q", n)
			}
		}
		if len(got) != len(want) {
			t.Fatalf("carried %d files, want %d\n got %v\nwant %v", len(got), len(want), keys(got), keys(want))
		}
		for n, text := range want {
			if got[n] != text {
				t.Fatalf("%s = %q, want %q", n, got[n], text)
			}
		}
	}
}

func keys(m map[string]string) []string {
	var k []string
	for n := range m {
		k = append(k, n)
	}
	sort.Strings(k)
	return k
}

func TestCatalogueHasOneEntryForEachTaskInOrderAndPinsClones(t *testing.T) {
	staged := []assemble.Staged{
		{Entry: entry("examples/z/last"), Tasks: []string{"b", "a"}},
		{Entry: assemble.Entry{Path: "examples/m/repositories/ext", External: true}, Tasks: []string{"default"}, Pin: &assemble.Pin{URL: "https://example.invalid/e.git", Revision: rev}},
	}
	cards := &fakeCards{}
	c, err := assemble.Catalogue(t.Context(), "3.1.4", stageDir, staged, cards)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, e := range c.GetExamples() {
		got = append(got, e.GetExample()+":"+e.GetCard().GetTask()+":"+e.GetFolder()+":"+e.GetTree())
		if e.GetNeedsClone() != (e.GetClone() != nil) {
			t.Errorf("%s: needs_clone %v with clone %v", e.GetExample(), e.GetNeedsClone(), e.GetClone())
		}
	}
	want := []string{
		"m/repositories/ext:default:ext:examples/m/repositories/ext",
		"z/last:a:last:examples/z/last",
		"z/last:b:last:examples/z/last",
	}
	if !slices.Equal(got, want) {
		t.Fatalf("entries %v, want %v", got, want)
	}
	if c.GetRelease() != "3.1.4" {
		t.Fatalf("release %q", c.GetRelease())
	}
	// The card reader is asked about the staged folder, in the stage's own absolute path, which the
	// catalogue never holds.
	if !slices.Contains(cards.asked, stageDir+"/examples/z/last:a") {
		t.Fatalf("asked %v", cards.asked)
	}
	data, err := release.MarshalCatalogue(c)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(data, []byte(stageDir)) {
		t.Fatal("the catalogue holds the stage's absolute path")
	}
}

func TestCatalogueFailsNamingTheTaskTheCLICannotRead(t *testing.T) {
	staged := []assemble.Staged{{Entry: entry("examples/a/b"), Tasks: []string{"ok", "bad"}}}
	cards := &fakeCards{fail: map[string]error{stageDir + "/examples/a/b:bad": errors.New("vibesys.input.toml: unknown key x")}}
	_, err := assemble.Catalogue(t.Context(), "1.0.0", stageDir, staged, cards)
	if err == nil || !strings.Contains(err.Error(), "a/b") || !strings.Contains(err.Error(), "bad") || !strings.Contains(err.Error(), "unknown key x") {
		t.Fatalf("err = %v", err)
	}
	cards = &fakeCards{renamed: map[string]string{stageDir + "/examples/a/b:ok": "other"}}
	if _, err := assemble.Catalogue(t.Context(), "1.0.0", stageDir, staged, cards); err == nil {
		t.Fatal("a card of another task was accepted")
	}
	// The same task name in two examples is two entries; the same example listing a task twice is not.
	twice := []assemble.Staged{{Entry: entry("examples/a/b"), Tasks: []string{"t", "t"}}}
	if _, err := assemble.Catalogue(t.Context(), "1.0.0", stageDir, twice, &fakeCards{}); err == nil {
		t.Fatal("a task listed twice was accepted")
	}
	same := []assemble.Staged{{Entry: entry("examples/a/b"), Tasks: []string{"t"}}, {Entry: entry("examples/a/c"), Tasks: []string{"t"}}}
	if _, err := assemble.Catalogue(t.Context(), "1.0.0", stageDir, same, &fakeCards{}); err != nil {
		t.Fatal(err)
	}
}

// What the builder lays out is a release the host can read: every catalogue tree is there, each
// example's source is served, and the pinned clone is the one the repository pins.
func TestWhatIsBuiltIsAReleaseTheHostReads(t *testing.T) {
	repo := fstest.MapFS{
		"examples/db/kv/.vibesys/tasks/t/vibesys.input.toml": file("t"),
		"examples/db/kv/src/lib.rs":                          file("lib"),
	}
	ext := &fakeExternals{
		own:  map[string]fs.FS{"examples/ms/repositories/vllm": fstest.MapFS{".vibesys/tasks/default/vibesys.input.toml": file("d")}},
		pins: map[string]assemble.Pin{"examples/ms/repositories/vllm": {URL: "https://example.invalid/vllm.git", Revision: rev}},
	}
	sink := newSink()
	entries := []assemble.Entry{entry("examples/db/kv"), {Path: "examples/ms/repositories/vllm", External: true}}
	staged, err := assemble.Stage(t.Context(), repo, entries, ext, sink)
	if err != nil {
		t.Fatal(err)
	}
	c, err := assemble.Catalogue(t.Context(), "1.0.0", stageDir, staged, &fakeCards{})
	if err != nil {
		t.Fatal(err)
	}
	data, err := release.MarshalCatalogue(c)
	if err != nil {
		t.Fatal(err)
	}
	tree := releasetest.Tree(t, "/opt/vs", releasetest.Content(release.CatalogueName, string(data)))
	for name, f := range sink.files {
		tree["opt/vs/"+name] = f
	}
	r, err := release.Open("/opt/vs", tree)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Verify(release.Archived); err != nil {
		t.Fatal(err)
	}
	own, err := r.ExampleSource("db/kv")
	if err != nil || own.Clone != nil {
		t.Fatalf("db/kv: %+v, %v", own, err)
	}
	if b, err := fs.ReadFile(own.Vendored, "src/lib.rs"); err != nil || string(b) != "lib" {
		t.Fatalf("src/lib.rs = %q, %v", b, err)
	}
	cloned, err := r.ExampleSource("ms/repositories/vllm")
	if err != nil || cloned.Clone == nil || cloned.Clone.Revision != rev || cloned.Clone.URL != "https://example.invalid/vllm.git" {
		t.Fatalf("ms/repositories/vllm: %+v, %v", cloned, err)
	}
	// Nothing private ships: only the example folders, the catalogue and the base release are there.
	if err := fs.WalkDir(tree, ".", func(name string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if path.Base(name) == ".git" || strings.Contains(name, "/repositories/vllm/vllm") {
			t.Errorf("ships %s", name)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

// The repository's own registry and examples: every runnable example of the repository is staged,
// every task of it has a catalogue entry that the catalogue's rules accept, and nothing the stage
// holds is a repository, a link or an absolute name. The examples that live in other repositories
// are given a fixed tree, which is all the registry's pin can say without the network.
func TestTheRepositorysExamplesBecomeAValidCatalogue(t *testing.T) {
	repo := os.DirFS("../../..")
	data, err := fs.ReadFile(repo, assemble.RegistryName)
	if err != nil {
		t.Fatal(err)
	}
	entries, err := assemble.ParseRegistry(data)
	if err != nil {
		t.Fatal(err)
	}
	ext := &fakeExternals{own: map[string]fs.FS{}, pins: map[string]assemble.Pin{}}
	for _, e := range entries {
		if e.External {
			ext.own[e.Path] = fstest.MapFS{".vibesys/tasks/default/vibesys.input.toml": file("t")}
			ext.pins[e.Path] = assemble.Pin{URL: "https://example.invalid/" + path.Base(e.Path) + ".git", Revision: rev}
		}
	}
	sink := newSink()
	staged, err := assemble.Stage(t.Context(), repo, entries, ext, sink)
	if err != nil {
		t.Fatal(err)
	}
	c, err := assemble.Catalogue(t.Context(), "1.0.0", stageDir, staged, &fakeCards{})
	if err != nil {
		t.Fatal(err)
	}
	tasks := 0
	for _, s := range staged {
		tasks += len(s.Tasks)
	}
	if len(entries) < 30 || len(c.GetExamples()) != tasks || tasks < len(entries) {
		t.Fatalf("%d registry entries, %d tasks, %d catalogue entries", len(entries), tasks, len(c.GetExamples()))
	}
	// Each in-tree example's every task folder on disk is in the catalogue: a task added to a listed
	// example appears without any other edit.
	for _, e := range entries {
		if e.External || e.Tasks != nil {
			continue
		}
		dirs, err := fs.ReadDir(repo, path.Join(e.Path, ".vibesys/tasks"))
		if err != nil {
			t.Fatalf("%s: %v", e.Path, err)
		}
		for _, d := range dirs {
			if d.IsDir() && !slices.ContainsFunc(c.GetExamples(), func(x *pb.ReleaseExample) bool { return x.GetExample() == e.ID() && x.GetCard().GetTask() == d.Name() }) {
				t.Errorf("%s task %s has no catalogue entry", e.ID(), d.Name())
			}
		}
	}
	for name, f := range sink.files {
		if path.IsAbs(name) || path.Base(name) == ".git" || f.Mode&fs.ModeSymlink != 0 {
			t.Errorf("stage holds %s", name)
		}
	}
	for _, e := range c.GetExamples() {
		if e.GetNeedsClone() && !strings.Contains(e.GetExample(), "repositories/") {
			t.Errorf("%s needs a clone but is not a repository example", e.GetExample())
		}
	}
	if _, err := release.MarshalCatalogue(c); err != nil {
		t.Fatal(err)
	}
}

func TestRegistryParsing(t *testing.T) {
	good := `version = 1
[[example]]
path = "examples/b/two"
tasks = ["x", "y"]
known_failing = [{ check = "c", reason = "r", tracking = "#1" }]
[[example]]
path = "examples/a/one"
tasks = "all"
external_repo = true
skips = [{ check = "c", reason = "r" }]
`
	entries, err := assemble.ParseRegistry([]byte(good))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 || entries[0].Path != "examples/a/one" || !entries[0].External || entries[0].Tasks != nil ||
		entries[1].ID() != "b/two" || !slices.Equal(entries[1].Tasks, []string{"x", "y"}) {
		t.Fatalf("entries = %+v", entries)
	}
	bad := map[string]string{
		"unknown key":   "version = 1\n[[example]]\npath = \"examples/a/b\"\nexternal = true\n",
		"version":       "version = 2\n",
		"no version":    "[[example]]\npath = \"examples/a/b\"\n",
		"outside":       "version = 1\n[[example]]\npath = \"src/a\"\n",
		"climbs":        "version = 1\n[[example]]\npath = \"examples/../src\"\n",
		"root":          "version = 1\n[[example]]\npath = \"examples/\"\n",
		"twice":         "version = 1\n[[example]]\npath = \"examples/a/b\"\n[[example]]\npath = \"examples/a/b\"\n",
		"tasks word":    "version = 1\n[[example]]\npath = \"examples/a/b\"\ntasks = \"some\"\n",
		"tasks empty":   "version = 1\n[[example]]\npath = \"examples/a/b\"\ntasks = []\n",
		"tasks numbers": "version = 1\n[[example]]\npath = \"examples/a/b\"\ntasks = [1]\n",
		"not toml":      "version = ",
	}
	for name, text := range bad {
		if _, err := assemble.ParseRegistry([]byte(text)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := assemble.ParseRegistry([]byte(bad["unknown key"])); err == nil || !strings.Contains(err.Error(), "example.external") {
		t.Errorf("the unknown key is not named: %v", err)
	}
}

func TestSubmodulePinsAreReadFromGitsOutput(t *testing.T) {
	config := "submodule.examples/a/repositories/x.path examples/a/repositories/x\n" +
		"submodule.examples/a/repositories/x.url https://example.invalid/x.git\n" +
		"submodule.third.party/y.path vendor/y\n" +
		"submodule.third.party/y.url https://example.invalid/y.git\n" +
		"submodule.nourl.path examples/a/repositories/nourl\n"
	if u, err := assemble.SubmoduleURL(config, "examples/a/repositories/x"); err != nil || u != "https://example.invalid/x.git" {
		t.Fatalf("x: %q, %v", u, err)
	}
	if u, err := assemble.SubmoduleURL(config, "vendor/y"); err != nil || u != "https://example.invalid/y.git" {
		t.Fatalf("y: %q, %v", u, err)
	}
	for _, p := range []string{"examples/a/repositories/nourl", "examples/a/repositories/none", ""} {
		if _, err := assemble.SubmoduleURL(config, p); err == nil {
			t.Errorf("%q: accepted", p)
		}
	}
	line := "160000 commit " + rev + "\texamples/a/repositories/x\n"
	if c, err := assemble.Gitlink(line, "examples/a/repositories/x"); err != nil || c != rev {
		t.Fatalf("gitlink: %q, %v", c, err)
	}
	for _, bad := range []string{"", "040000 tree " + rev + "\texamples/a/repositories/x\n", "100644 blob " + rev + "\texamples/a/repositories/x\n", line + line, "160000 commit " + rev + "\tother\n"} {
		if _, err := assemble.Gitlink(bad, "examples/a/repositories/x"); err == nil {
			t.Errorf("%q: accepted", bad)
		}
	}
}
