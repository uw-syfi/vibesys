package filestore_test

import (
	"reflect"
	"strings"
	"testing"

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

// A store of version 2 opens as it was, and is a store of version 4 from then on: its format file
// says 4, it holds a frontier, and a program of version 2 would refuse it. A crash at any step of
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
			if got := unspaced(file(t, mem, "state/format")); got != `{"format":4}` {
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
			if got := unspaced(file(t, mem.CrashImage(power), "state/format")); got != `{"format":2}` && got != `{"format":4}` {
				t.Fatalf("format file after a crash at step %d = %s", step, got)
			}
		}
	}
}

// A store of version 3 opens as it was, and is a store of version 4 from then on: version 4 only
// added a job's placement, so every record of version 3 reads as it was written.
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
	if got := unspaced(file(t, image, "state/format")); got != `{"format":4}` {
		t.Fatalf("format file after the migration = %s", got)
	}
	if got := storetest.Observe(t, opened, runs...); !reflect.DeepEqual(got, want) {
		t.Fatalf("a migrated store shows\n%+v\nwant\n%+v", got, want)
	}
}
