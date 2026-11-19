package filestore_test

import (
	"fmt"
	"reflect"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

// planFieldsAddedBy names, for each field of a plan, the format version the store was at when the
// field was added. A field added to Plan is named here under the version that adds it, and that
// version is the one the program writes, so a program of an older version refuses the store by its
// format file instead of failing on the first plan that sets the field.
var planFieldsAddedBy = map[string]int{
	"number": 2, "title": 2, "body": 2, "status": 2, "comments": 2, "created_at": 2, "updated_at": 2,
	"gain_bound": 5,
	"workstream": 6, "origin": 6, "transitions": 6,
	"kind": 9, "depends_on": 9,
}

// Every field of a plan is named under the version that added it, and no version named is newer
// than the one a fresh store is written at.
func TestEveryFieldOfAPlanIsWrittenAtAVersionThatKnowsIt(t *testing.T) {
	mem := fsys.NewMemFS()
	if err := openOn(t, mem).Close(); err != nil {
		t.Fatal(err)
	}
	version := formatOf(t, mem)
	fields := (&cliv1.Plan{}).ProtoReflect().Descriptor().Fields()
	for i := range fields.Len() {
		name := string(fields.Get(i).Name())
		added, ok := planFieldsAddedBy[name]
		switch {
		case !ok:
			t.Errorf("Plan.%s is not in planFieldsAddedBy: name the format version that adds it, and raise the store's format if it is newer", name)
		case added > version:
			t.Errorf("Plan.%s was added in format %d, and a fresh store is written at format %d: a program of format %d would open the store and refuse its plans", name, added, version, version)
		}
	}
}

// A store of version 8, written before a plan had a kind or dependencies, opens as a store of the
// current version, and each of its plans reads as one whose planner stated neither, and so is ready
// while it is open. The migrated store then takes kinds and dependencies, and they read back the same
// after it is opened again: the migration round-trips.
func TestAStoreOfVersionEightReadsItsPlansWithNoKindOrDependencies(t *testing.T) {
	image := populated(t)
	for name, data := range image.Files() {
		if strings.HasPrefix(string(data), "{") && !strings.HasSuffix(name, ".lock") {
			image.Put(name, withVersion(data, "8"))
		}
	}
	opened := openOn(t, image)
	if got := formatOf(t, image); got != 10 {
		t.Fatalf("format after the migration = %d, want 10", got)
	}
	plans, err := opened.Plans(t.Context(), core.PlanFilter{})
	if err != nil || len(plans) == 0 {
		t.Fatalf("Plans = %v, %v", plans, err)
	}
	for _, p := range plans {
		if p.Kind != "" || p.DependsOn != nil {
			t.Errorf("plan %d of a store of version 8 reads with kind %q and dependencies %v", p.Number, p.Kind, p.DependsOn)
		}
	}

	kind := core.PlanKernel
	if _, err := opened.EditPlan(t.Context(), 1, core.PlanEdit{Kind: &kind, Origin: core.RecordOrigin{At: at(50)}}); err != nil {
		t.Fatal(err)
	}
	second, err := opened.CreatePlan(t.Context(), core.NewPlan{
		Title: "merge the kernels", Kind: core.PlanIntegration, DependsOn: []core.PlanNumber{1}, Origin: core.RecordOrigin{At: at(51)},
	})
	if err != nil {
		t.Fatal(err)
	}
	if second.Kind != core.PlanIntegration || !reflect.DeepEqual(second.DependsOn, []core.PlanNumber{1}) {
		t.Fatalf("CreatePlan = %+v, want an integration plan that depends on plan 1", second)
	}
	want, err := opened.Plans(t.Context(), core.PlanFilter{})
	if err != nil {
		t.Fatal(err)
	}
	if err := opened.Close(); err != nil {
		t.Fatal(err)
	}
	if f := planFile(t, image, 2); f.GetFormat() != 10 {
		t.Errorf("plan 2 is written at format %d, want 10", f.GetFormat())
	}
	again := openOn(t, image.CrashImage(false))
	got, err := again.Plans(t.Context(), core.PlanFilter{})
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("after a reopen Plans = %+v, %v\nwant %+v", got, err, want)
	}
}

// planFile reads plan n's file.
func planFile(t *testing.T, m *fsys.MemFS, n int) *cliv1.PlanFile {
	t.Helper()
	var record cliv1.PlanFile
	if err := protojson.Unmarshal(file(t, m, fmt.Sprintf("state/plans/%d.json", n)), &record); err != nil {
		t.Fatal(err)
	}
	return &record
}

// putPlanFile writes a plan's file under its number, as the program does.
func putPlanFile(t *testing.T, m *fsys.MemFS, record *cliv1.PlanFile) {
	t.Helper()
	data, err := (protojson.MarshalOptions{UseProtoNames: true}).Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	m.Put(fmt.Sprintf("state/plans/%d.json", record.GetPlan().GetNumber()), data)
}
