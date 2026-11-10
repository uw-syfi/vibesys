package filestore_test

import (
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// versionTwo is the filesystem of a store as a program of format version 2 left it: the script
// without its frontier changes, every record stamped 2, and no frontier directory.
func versionTwo(t *testing.T) *fsys.MemFS {
	t.Helper()
	mem := fsys.NewMemFS()
	s := openOn(t, mem)
	for _, o := range script() {
		if strings.HasPrefix(o.name, "admit") {
			continue
		}
		if err := o.run(t.Context(), s); err != nil {
			t.Fatalf("%s: %v", o.name, err)
		}
	}
	image := mem.CrashImage(false)
	for name, data := range image.Files() {
		if strings.HasPrefix(string(data), "{") && !strings.HasSuffix(name, ".lock") {
			image.Put(name, withVersion(data, "2"))
		}
	}
	if err := image.Remove("state/frontier"); err != nil {
		t.Fatalf("removing the frontier directory: %v", err)
	}
	return image
}

// A store of version 2 opens as it was, and is a store of version 6 from then on: its format file
// says 5, it holds a frontier, and a program of version 2 would refuse it. A crash at any step of
// the migration leaves a store that the next Open migrates and that shows the same.
func TestAStoreOfVersionTwoIsMigrated(t *testing.T) {
	ref := versionTwo(t)
	s := openOn(t, ref.CrashImage(false))
	want := storetest.Observe(t, s, runs...)
	if f, err := s.Frontier(t.Context()); err != nil || f.Revision != 0 {
		t.Fatalf("Frontier of a migrated store = %+v, %v", f, err)
	}
	if _, err := s.UpdateFrontier(t.Context(), core.AssumeAdmitted(0, []core.FrontierMember{{Commit: "c1", AddedAt: at(50)}})); err != nil {
		t.Fatalf("UpdateFrontier on a migrated store: %v", err)
	}

	for step := 0; ; step++ {
		mem := ref.CrashImage(false)
		mem.SetFault(fsys.Fault{At: step, Kind: fsys.FaultCrash})
		opened, err := filestore.Open(stateDir, filestore.WithFS(mem))
		if err == nil {
			if got := unspaced(file(t, mem, "state/format")); got != `{"format":7}` {
				t.Fatalf("format file after the migration = %s", got)
			}
			if got := storetest.Observe(t, opened, runs...); !reflect.DeepEqual(got, want) {
				t.Fatalf("a migrated store shows\n%+v\nwant\n%+v", got, want)
			}
			if step == 0 {
				t.Fatal("the migration took no step")
			}
			return
		}
		for _, power := range []bool{false, true} {
			again := openOn(t, mem.CrashImage(power))
			if got := storetest.Observe(t, again, runs...); !reflect.DeepEqual(got, want) {
				t.Fatalf("after a crash at step %d (power loss %v) the store shows\n%+v\nwant\n%+v", step, power, got, want)
			}
			if got := unspaced(file(t, mem.CrashImage(power), "state/format")); got != `{"format":2}` && got != `{"format":7}` {
				t.Fatalf("format file after a crash at step %d = %s", step, got)
			}
		}
	}
}

// A store of version 3 opens as it was, and is a store of version 6 from then on: version 4 only
// added a job's placement and version 5 a job's needs, so every record of version 3 reads as it
// was written. Its results run in no docker environment, so they keep their keys.
func TestAStoreOfVersionThreeIsMigrated(t *testing.T) {
	mem := fsys.NewMemFS()
	s := openOn(t, mem)
	for _, o := range script() {
		if err := o.run(t.Context(), s); err != nil {
			t.Fatalf("%s: %v", o.name, err)
		}
	}
	want := storetest.Observe(t, s, runs...)
	image := mem.CrashImage(false)
	for name, data := range image.Files() {
		if strings.HasPrefix(string(data), "{") && !strings.HasSuffix(name, ".lock") {
			image.Put(name, withVersion(data, "3"))
		}
	}
	opened := openOn(t, image)
	if got := unspaced(file(t, image, "state/format")); got != `{"format":7}` {
		t.Fatalf("format file after the migration = %s", got)
	}
	if got := storetest.Observe(t, opened, runs...); !reflect.DeepEqual(got, want) {
		t.Fatalf("a migrated store shows\n%+v\nwant\n%+v", got, want)
	}
}

// fungibleCommit is the commit the version 4 store below checked on two placements.
const fungibleCommit core.Commit = "0123456789abcdef0123456789abcdef01234567"

// onPlacement is a docker environment of one image on the placement.
func onPlacement(name string) *core.Fingerprint {
	return &core.Fingerprint{Kind: core.EnvironmentDocker, Backend: core.BackendCPU, ImageDigest: "sha256:" + strings.Repeat("ab", 32), Facts: map[string]string{
		core.FactPlacement: name, "os": "linux", "arch": "amd64",
	}}
}

// checkedOn saves a check pass of fungibleCommit taken on the placement, under the key.
func checkedOn(t *testing.T, s *filestore.Store, placement string, key core.JobKey, n int) core.JobID {
	t.Helper()
	out, err := s.Submit(t.Context(), core.Submission{Spec: core.JobSpec{Commit: fungibleCommit, Environment: core.EnvironmentDocker, Settings: core.CheckSettings{}}, Key: key, NoCache: true, At: at(n)})
	if err != nil {
		t.Fatal(err)
	}
	id, env := out.Job.ID, onPlacement(placement)
	if _, err := s.Transition(t.Context(), core.Transition{Job: id, From: core.JobQueued, To: core.JobRunning, At: at(n + 1), Environment: env}); err != nil {
		t.Fatal(err)
	}
	result := &core.Result{
		Job: id, Key: key, Task: "kv-store", TaskVersion: "v1", Commit: fungibleCommit, SchemaVersion: core.SchemaVersion, Environment: *env,
		RecordedAt: at(n + 2), Outcome: core.CheckOutcome{Duration: time.Second},
	}
	if _, err := s.Transition(t.Context(), core.Transition{Job: id, From: core.JobRunning, To: core.JobSucceeded, At: at(n + 2), Result: result}); err != nil {
		t.Fatal(err)
	}
	return id
}

// A store of version 4 opens as a store of version 6, in which where a docker result ran takes no
// part in its key: two passes of one commit and image that a version 4 program keyed apart, as they
// ran on two placements, answer the one key the current rule gives them, as do the jobs that saved
// them, and a check of that key, submitted from any placement, reuses them. The records are read,
// not rewritten. A record of version 5 keeps the key it was saved under.
func TestAStoreOfVersionFourHasWhereItsResultsRanLeaveTheirKeys(t *testing.T) {
	mem := fsys.NewMemFS()
	s := openOn(t, mem)
	a := checkedOn(t, s, "a", "old-key-on-a", 1)
	b := checkedOn(t, s, "b", "old-key-on-b", 10)
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	image := mem.CrashImage(false)
	for name, data := range image.Files() {
		if strings.HasPrefix(string(data), "{") && !strings.HasSuffix(name, ".lock") {
			image.Put(name, withVersion(data, "4"))
		}
	}
	want, err := core.NewJobKey(core.KeyInputs{
		Kind: core.JobCheck, Commit: fungibleCommit, TaskVersion: "v1", Environment: *onPlacement("elsewhere"), Settings: core.CheckSettings{}, SchemaVersion: core.SchemaVersion,
	})
	if err != nil {
		t.Fatal(err)
	}

	opened := openOn(t, image)
	if got := unspaced(file(t, image, "state/format")); got != `{"format":7}` {
		t.Fatalf("format file after the migration = %s", got)
	}
	results, err := opened.Results(t.Context(), core.ResultFilter{Key: want})
	if err != nil || len(results) != 2 {
		t.Fatalf("results under the key of the commit and image = %+v, %v; want the passes of both placements", results, err)
	}
	for _, r := range results {
		if r.Environment.Placement() != map[core.JobID]string{a: "a", b: "b"}[r.Job] {
			t.Fatalf("the result of %s says it ran on %q", r.Job, r.Environment.Placement())
		}
	}
	for _, id := range []core.JobID{a, b} {
		if job, err := opened.Job(t.Context(), id); err != nil || job.Key != want {
			t.Fatalf("job %s has key %q, %v; want %q", id, job.Key, err, want)
		}
	}
	out, err := opened.Submit(t.Context(), core.Submission{Spec: core.JobSpec{Commit: fungibleCommit, Environment: core.EnvironmentDocker, Settings: core.CheckSettings{}}, Key: want, At: at(30)})
	if err != nil || out.Disposition != core.SubmitReused {
		t.Fatalf("a check of the commit and image: %+v, %v; want it to reuse a pass", out, err)
	}

	current := fsys.NewMemFS()
	s = openOn(t, current)
	checkedOn(t, s, "a", "kept-key", 1)
	if results, err := s.Results(t.Context(), core.ResultFilter{Key: "kept-key"}); err != nil || len(results) != 1 {
		t.Fatalf("a result of version 5 under its own key: %+v, %v", results, err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	if results, err := openOn(t, current.CrashImage(false)).Results(t.Context(), core.ResultFilter{Key: "kept-key"}); err != nil || len(results) != 1 {
		t.Fatalf("a result of version 5 reopened: %+v, %v; want it under the key it was saved with", results, err)
	}
}
