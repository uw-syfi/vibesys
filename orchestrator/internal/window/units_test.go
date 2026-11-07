package window_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// An objective moves in the unit its measurements record: the one they all share, and none (unknown)
// when any of them records none or two record different ones, since no unit can then be stated.
func TestAnObjectiveMovesInTheUnitOfItsMeasurements(t *testing.T) {
	r := rand.New(rand.NewPCG(4, 5))
	pool := []string{"", "tok/s", "tok/s", "ms"}
	for range 300 {
		var facts []*cli.TaskFact
		var units []string
		for i := range 1 + r.IntN(5) {
			id := fmt.Sprintf("j%d", i)
			unit := pool[r.IntN(len(pool))]
			m := &cli.MeasureResult{Metric: &cli.Metric{Name: "rate", Direction: cli.Direction_DIRECTION_MAXIMIZE, Unit: unit}, Value: float64(1 + i)}
			facts = append(facts,
				&cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, Submitters: []*cli.Submitter{{RunId: string(ours), At: timestamppb.New(t0)}}}}},
				&cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id, Commit: "c" + id, RecordedAt: timestamppb.New(at(i + 1)), Outcome: &cli.Result_Measure{Measure: m}}}})
		}
		got := factsOf(t, 0, grid, facts...)
		if len(got.Objectives) != 1 {
			t.Fatalf("objectives %+v", got.Objectives)
		}
		// The oracle: of the units the run recorded, the newest known one decides which points stay.
		newest := ""
		for _, f := range facts {
			if m := f.GetResult().GetMeasure(); m != nil && m.GetMetric().GetUnit() != "" {
				newest = m.GetMetric().GetUnit()
			}
		}
		for _, f := range facts {
			if m := f.GetResult().GetMeasure(); m != nil && (m.GetMetric().GetUnit() == "" || m.GetMetric().GetUnit() == newest) {
				units = append(units, m.GetMetric().GetUnit())
			}
		}
		want := ""
		if !slices.ContainsFunc(units, func(u string) bool { return u != units[0] }) {
			want = units[0]
		}
		if got.Objectives[0].Unit != want {
			t.Fatalf("units %q: the move says %q, want %q", units, got.Objectives[0].Unit, want)
		}
	}
}
