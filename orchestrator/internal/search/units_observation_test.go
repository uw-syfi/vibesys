package search_test

import (
	"strings"
	"testing"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// An objective other than the scored metric has the unit its observation records, which the series and
// the bound's check read as they read the scored metric's.
func TestAnObservedObjectiveHasTheUnitItsObservationRecords(t *testing.T) {
	for _, tc := range []struct {
		bound string
		want  pb.BoundUnitCheck
	}{{"ms", pb.BoundUnitCheck_BOUND_UNIT_CHECK_MATCH}, {"s", pb.BoundUnitCheck_BOUND_UNIT_CHECK_MISMATCH}} {
		m := &cli.MeasureResult{
			Metric: &cli.Metric{Name: "rate", Direction: maximize, Unit: "tok/s"}, Value: 5,
			Objectives:   &cli.Objectives{Metrics: []*cli.Metric{{Name: "rate", Direction: maximize}, {Name: "latency", Direction: minimize}}},
			Observations: []*cli.Observation{{Name: "latency", Value: proto.Float64(40), Unit: "ms"}},
		}
		facts := []*cli.TaskFact{
			{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1", Submitters: mine}}},
			{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: "j1", Commit: strings.Repeat("c", 40), RecordedAt: &timestamppb.Timestamp{Seconds: 1}, Outcome: &cli.Result_Measure{Measure: m}}}},
			boundFact(1, "r", "latency", minimize, 10),
		}
		facts[2].GetBound().Unit = tc.bound
		s := project(t, facts...)
		if got := seriesNamed(s, "latency").Unit; got != "ms" {
			t.Fatalf("the series of the observed objective is in %q, want ms", got)
		}
		if got := byObjective(s, "latency"); got.UnitCheck != tc.want || got.MeasuredUnit != "ms" {
			t.Fatalf("bound in %s: %v measured in %q, want %v in ms", tc.bound, got.UnitCheck, got.MeasuredUnit, tc.want)
		}
	}
}
