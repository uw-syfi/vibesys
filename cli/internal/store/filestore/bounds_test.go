package filestore_test

import (
	"path"
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

// A store made before declared bounds existed has no directory for them. It reads as one with no
// bound declared, the first declaration makes the directory, and what was declared survives a
// reopening, so no format version has to separate the two builds.
func TestAStoreMadeBeforeBoundsHasNoneAndTakesThem(t *testing.T) {
	mem := fsys.NewMemFS()
	if err := openOn(t, mem).Close(); err != nil {
		t.Fatal(err)
	}
	if err := mem.Remove(path.Join(stateDir, "bounds")); err != nil {
		t.Fatalf("removing the bounds directory: %v", err)
	}
	s := openOn(t, mem)
	if got, err := s.Bounds(t.Context()); err != nil || len(got) != 0 {
		t.Fatalf("Bounds of a store with no directory = %+v, %v; want none", got, err)
	}
	declared, err := s.DeclareBound(t.Context(), core.NewBound{
		Origin:    core.RecordOrigin{Run: "run-a", Agent: "lead", At: at(1)},
		Objective: "ops", Direction: core.Maximize, Value: 15000, Unit: "ops/s", Note: "bandwidth bound",
	})
	if err != nil || declared.Seq != 1 {
		t.Fatalf("DeclareBound = %+v, %v; want place 1", declared, err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	for _, power := range []bool{false, true} {
		got, err := openOn(t, mem.CrashImage(power)).Bounds(t.Context())
		if err != nil || !reflect.DeepEqual(got, []core.Bound{declared}) {
			t.Errorf("after reopening (power loss %v) Bounds = %+v, %v; want %+v", power, got, err, declared)
		}
	}
}
