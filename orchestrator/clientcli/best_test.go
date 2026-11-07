package clientcli_test

import (
	"context"
	"strings"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func seriesWith(objective, unit string, values ...float64) *pb.ObjectiveSeries {
	s := &pb.ObjectiveSeries{Objective: objective, Direction: cliv1.Direction_DIRECTION_MAXIMIZE, Unit: unit}
	top := 0.0
	for i, v := range values {
		s.Points = append(s.Points, &pb.MeasuredPoint{JobId: "j", Commit: "c", At: timestamppb.New(epoch), Value: v, BestSoFar: i == 0 || v > top})
		top = max(top, v)
	}
	return s
}

// `run get` says two things on two rows: the head, the value of the candidate the run last promoted
// on the metric it scored, and the best, which the search state serves for each objective (the bound's
// best value, or where the series' best line ends). A head that is not the best is never labelled best,
// and each value is followed by the unit it is counted in.
func TestGetSaysTheHeadAndTheBestApartWithTheirUnits(t *testing.T) {
	head := &cliv1.Head{State: &cliv1.Head_Current{Current: &cliv1.CurrentResult{Head: strings.Repeat("a", 40), Result: &cliv1.Result{Outcome: &cliv1.Result_Measure{Measure: &cliv1.MeasureResult{
		Metric: &cliv1.Metric{Name: "tput", Direction: cliv1.Direction_DIRECTION_MAXIMIZE, Unit: "tok/s"}, Value: 60,
	}}}}}}
	state := &pb.SearchState{
		RunId: "run-1", Revision: 1, Head: head,
		Health: &pb.SearchHealth{State: &pb.SearchHealth_LiveSince{LiveSince: timestamppb.New(epoch)}},
		Series: []*pb.ObjectiveSeries{seriesWith("tput", "tok/s", 40, 90, 60), seriesWith("latency", "ms", 30, 12)},
		Bounds: []*pb.ObjectiveBounds{{
			Objective: "latency", Direction: cliv1.Direction_DIRECTION_MAXIMIZE, BestValue: ptrTo(12), UnitCheck: pb.BoundUnitCheck_BOUND_UNIT_CHECK_MISMATCH,
			Versions: []*cliv1.ObjectiveBound{{Objective: "latency", Direction: cliv1.Direction_DIRECTION_MAXIMIZE, Unit: "s"}},
		}},
	}
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	frame := snapshotWithSearch(5, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)}, state)
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))

	got := w.run(context.Background(), "run", "get", "run-1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "summary", got.stdout,
		"head        tput = 60 tok/s at aaaaaaa",
		"best        tput = 90 tok/s, latency = 12 ms (its bound is in s, unverified)")
}

func ptrTo(v float64) *float64 { return &v }
