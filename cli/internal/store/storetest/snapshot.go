package storetest

import (
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"testing"
)

func testSnapshot(t *testing.T, newStore New) {
	e := newEnv(t, newStore)
	before, err := e.s.Snapshot(e.ctx)
	if err != nil {
		t.Fatal(err)
	}
	result := SeedMeasurement(t, e.s, Measurement{Commit: "c1", Samples: []float64{1, 2, 3}, At: e.now()})
	after, err := e.s.Snapshot(e.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if after.Revision <= before.Revision || len(after.Jobs) != 1 || len(after.Results) != 1 ||
		after.Jobs[0].ResultJob != result.Job || after.Results[0].Job != result.Job {
		t.Fatalf("snapshot does not contain the whole transition: %+v", after)
	}
	after.Jobs[0].Spec.Commit = "changed"
	after.Results[0].Commit = "changed"
	same, err := e.s.Snapshot(e.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if same.Revision != after.Revision || same.Jobs[0].Spec.Commit != core.Commit("c1") || same.Results[0].Commit != core.Commit("c1") {
		t.Fatalf("snapshot aliases store or advances revision on read: %+v", same)
	}
}
