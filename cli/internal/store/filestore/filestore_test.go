package filestore_test

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/memstore"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// The contract suite runs on the in-memory filesystem, where it is fast, and on the real one,
// where the operations are the ones a user's disk performs.
func TestContractOnMemFS(t *testing.T) {
	storetest.Run(t, func(t *testing.T, rules store.Rules) store.Store {
		return openOn(t, fsys.NewMemFS(), filestore.WithRules(rules))
	})
}

func TestContractOnDisk(t *testing.T) {
	storetest.Run(t, func(t *testing.T, rules store.Rules) store.Store {
		s, err := filestore.Open(filepath.Join(t.TempDir(), "a", "state"), filestore.WithRules(rules))
		if err != nil {
			t.Fatal(err)
		}
		return s
	})
}

// A store reopened on the same directory answers as it did, and as a store that never closed.
func TestReopenedStoreAgreesWithMemstore(t *testing.T) {
	for seed := range uint64(12) {
		mem := fsys.NewMemFS()
		stores := []store.Store{memstore.New(), openOn(t, mem)}
		storetest.Differential(t, seed, 150, stores, func(step int) {
			switch step % 9 {
			case 3:
				if err := stores[1].(*filestore.Store).Close(); err != nil {
					t.Fatal(err)
				}
				stores[1] = openOn(t, mem)
			case 6:
				// What a power loss keeps: every call that returned was made durable.
				mem = mem.CrashImage(true)
				stores[1] = openOn(t, mem)
			}
		})
	}
}

func TestScriptSurvivesReopening(t *testing.T) {
	mem := fsys.NewMemFS()
	live := openOn(t, mem)
	for _, o := range script() {
		if err := o.run(t.Context(), live); err != nil {
			t.Fatalf("%s: %v", o.name, err)
		}
	}
	want := storetest.Observe(t, live, runs...)
	for _, power := range []bool{false, true} {
		got := storetest.Observe(t, openOn(t, mem.CrashImage(power)), runs...)
		if !reflect.DeepEqual(got, want) {
			t.Errorf("after a crash (power loss %v) the store shows\n%+v\nwant\n%+v", power, got, want)
		}
	}
}

func TestParallelDuplicateSubmissionsLandOnOneJobOnDisk(t *testing.T) {
	const n = 24
	dir := filepath.Join(t.TempDir(), "state")
	s, err := filestore.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	outs := make([]core.SubmitOutcome, n)
	errs := make([]error, n)
	storetest.Parallel(t, n, func(i int) {
		outs[i], errs[i] = s.Submit(t.Context(), core.Submission{Spec: measureSpec("c1"), Key: "k1", At: at(1)})
	})
	created := 0
	for i := range n {
		if errs[i] != nil {
			t.Fatalf("Submit %d: %v", i, errs[i])
		}
		if outs[i].Disposition == core.SubmitCreated {
			created++
		}
		if outs[i].Job.ID != "j1" {
			t.Errorf("submission %d landed on %s, want j1", i, outs[i].Job.ID)
		}
	}
	if created != 1 {
		t.Errorf("%d submissions created a job, want 1", created)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := filestore.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	jobs, err := reopened.Jobs(t.Context(), core.JobFilter{})
	if err != nil || len(jobs) != 1 {
		t.Fatalf("after reopening: %d jobs, %v; want 1", len(jobs), err)
	}
}

func TestOpenTaskKeepsAFileStorePerTask(t *testing.T) {
	repo := t.TempDir()
	a, err := filestore.OpenTask(repo, "task-a")
	if err != nil {
		t.Fatal(err)
	}
	b, err := filestore.OpenTask(repo, "task-b")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := a.Submit(t.Context(), core.Submission{Spec: measureSpec("c1"), Key: "k1", At: at(1)}); err != nil {
		t.Fatal(err)
	}
	if jobs, err := b.Jobs(t.Context(), core.JobFilter{}); err != nil || len(jobs) != 0 {
		t.Errorf("task-b sees %d jobs, %v; want none", len(jobs), err)
	}
	if err := a.Close(); err != nil {
		t.Fatal(err)
	}
	again, err := filestore.OpenTask(repo, "task-a")
	if err != nil {
		t.Fatal(err)
	}
	if jobs, err := again.Jobs(t.Context(), core.JobFilter{}); err != nil || len(jobs) != 1 {
		t.Errorf("task-a after reopening has %d jobs, %v; want 1", len(jobs), err)
	}
	for _, bad := range []string{"", "../x", "a/b", ".hidden"} {
		if _, err := filestore.OpenTask(repo, bad); err == nil {
			t.Errorf("OpenTask(%q) succeeded", bad)
		}
	}
}

// unreadable is a filesystem on which one directory cannot be listed, as a disk that answers
// with an I/O error.
type unreadable struct {
	fsys.FS
	dir string
}

func (u unreadable) ReadDir(name string) ([]fsys.Entry, error) {
	if name == u.dir {
		return nil, syscall.EIO
	}
	return u.FS.ReadDir(name)
}

// Only a store whose place is not there is marked gone, which is what lets a repository be
// forgotten: one that cannot be read, or that another daemon holds, may be whole.
func TestOnlyAStoreWhosePlaceIsNotThereIsGone(t *testing.T) {
	repo := t.TempDir()
	missing := filepath.Join(repo, "deleted")
	if _, err := filestore.OpenTask(missing, "task"); !errors.Is(err, store.ErrGone) || !errors.Is(err, store.ErrAway) {
		t.Errorf("a repository that is not there: %v, want it gone and away", err)
	}
	if _, err := filestore.OpenTask(repo, "task", filestore.WithFS(unreadable{fsys.OS{}, repo})); errors.Is(err, store.ErrGone) || !errors.Is(err, store.ErrAway) {
		t.Errorf("a repository that cannot be read: %v, want it away and not gone", err)
	}
	made, err := filestore.OpenTask(repo, "task", filestore.WithOwner(filestore.Owner{Host: "host-a", Dir: "/run/a"}))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := filestore.OpenTask(repo, "task", filestore.WithOwner(filestore.Owner{Host: "host-b", Dir: "/run/b"})); err == nil || errors.Is(err, store.ErrGone) {
		t.Errorf("a store another daemon holds: %v, want it refused and not gone", err)
	}
	if err := made.Close(); err != nil {
		t.Fatal(err)
	}
	// Everything the store made is deleted, and the repository stays.
	entries, err := os.ReadDir(repo)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if err := os.RemoveAll(filepath.Join(repo, e.Name())); err != nil {
			t.Fatal(err)
		}
	}
	entry := filepath.Join(repo, "the-index-entry")
	if _, err := filestore.OpenTask(repo, "task", filestore.Existing(entry)); !errors.Is(err, store.ErrGone) {
		t.Errorf("a store that was made and whose directory is not there: %v, want it gone", err)
	}
}

// ignoreFile is the .gitignore OpenTask keeps in a repository's state directory.
const ignoreFile = "repo/.vibesys-state/.gitignore"

func openTaskOn(t *testing.T, mem *fsys.MemFS, task string, opts ...filestore.Option) (*filestore.Store, error) {
	t.Helper()
	return filestore.OpenTask("repo", task, append([]filestore.Option{filestore.WithFS(mem)}, opts...)...)
}

// The state directory of a repository ignores itself for git, whether OpenTask made it or found it
// made by a program that wrote no .gitignore, and a .gitignore the user wrote there is kept as it is.
func TestTheStateDirectoryIsIgnoredByGit(t *testing.T) {
	mem := fsys.NewMemFS()
	if err := mem.Mkdir("repo"); err != nil {
		t.Fatal(err)
	}
	if _, err := openTaskOn(t, mem, "kv", filestore.LookOnly()); err == nil {
		t.Fatal("LookOnly opened a store that was never made")
	}
	if _, ok := mem.Files()[ignoreFile]; ok {
		t.Fatal("a look at a store that is not there wrote a .gitignore")
	}
	s, err := openTaskOn(t, mem, "kv")
	if err != nil {
		t.Fatal(err)
	}
	if got := string(mem.Files()[ignoreFile]); got != "*\n" {
		t.Fatalf("a new state directory has .gitignore %q, want %q", got, "*\n")
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}

	// A state directory an older program made has no .gitignore until its next open.
	if err := mem.Remove(ignoreFile); err != nil {
		t.Fatal(err)
	}
	for _, opts := range [][]filestore.Option{nil, {filestore.LookOnly()}} {
		s, err = openTaskOn(t, mem, "kv", opts...)
		if err != nil {
			t.Fatal(err)
		}
		if got := string(mem.Files()[ignoreFile]); got != "*\n" {
			t.Fatalf("an existing state directory has .gitignore %q after an open, want %q", got, "*\n")
		}
		if err := s.Close(); err != nil {
			t.Fatal(err)
		}
		if err := mem.Remove(ignoreFile); err != nil {
			t.Fatal(err)
		}
	}

	mem.Put(ignoreFile, []byte("jobs\n"))
	for _, task := range []string{"kv", "other"} {
		s, err := openTaskOn(t, mem, task)
		if err != nil {
			t.Fatal(err)
		}
		if err := s.Close(); err != nil {
			t.Fatal(err)
		}
	}
	if got := string(mem.Files()[ignoreFile]); got != "jobs\n" {
		t.Fatalf("the user's .gitignore became %q", got)
	}
	for name := range mem.Files() {
		if strings.HasPrefix(name, "repo/.vibesys-state/.tmp-") {
			t.Errorf("a temporary file is left: %s", name)
		}
	}
}

// A machine that dies at any step of the first open leaves the .gitignore whole or not there, and
// the next open leaves it whole.
func TestTheGitignoreIsWholeAfterACrashAtAnyStep(t *testing.T) {
	probe := fsys.NewMemFS()
	probe.Put("repo/README", nil)
	if _, err := openTaskOn(t, probe, "kv"); err != nil {
		t.Fatal(err)
	}
	for step := range probe.Steps() {
		for _, kind := range []fsys.FaultKind{fsys.FaultCrash, fsys.FaultTornWrite} {
			mem := fsys.NewMemFS()
			mem.Put("repo/README", nil)
			mem.SetFault(fsys.Fault{At: mem.Steps() + step, Kind: kind})
			_, _ = openTaskOn(t, mem, "kv")
			image := mem.CrashImage(kind == fsys.FaultTornWrite)
			if got, ok := image.Files()[ignoreFile]; ok && string(got) != "*\n" {
				t.Fatalf("step %d kind %d: after the crash .gitignore is %q", step, kind, got)
			}
			if _, err := openTaskOn(t, image, "kv"); err != nil {
				t.Fatalf("step %d kind %d: reopening: %v", step, kind, err)
			}
			if got := string(image.Files()[ignoreFile]); got != "*\n" {
				t.Fatalf("step %d kind %d: after reopening .gitignore is %q", step, kind, got)
			}
		}
	}
}
