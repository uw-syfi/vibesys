package filestore_test

import (
	"path/filepath"
	"reflect"
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
