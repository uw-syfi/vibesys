package search_test

import (
	"fmt"
	"math/rand"
	"strings"
	"testing"
	"testing/quick"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"google.golang.org/protobuf/proto"
)

// A run with no id matches no plan: a person's plan, which records no run, is not in it.
func TestARunWithNoIdHasNoIdeasOfItsOwn(t *testing.T) {
	plan := &cli.Plan{Number: 1, Origin: originOf("", 1), Transitions: []*cli.PlanTransition{{Status: cli.PlanStatus_PLAN_STATUS_OPEN, Origin: originOf("", 1)}}}
	s := search.Cap(search.Fold([]*cli.TaskFact{{Fact: &cli.TaskFact_Plan{Plan: plan}}}), &pb.SearchState{})
	if len(s.Ideas) != 1 || s.Ideas[0].InRun {
		t.Fatalf("a plan no run made is in the run with no id: %v", s.Ideas)
	}
}

// An idea lists at most 200 candidates, the newest, in the order of time, and the state says some
// were left out. Its gain is still the best of all of them.
func TestAnIdeaKeepsItsNewestCandidatesAndTheBestGainOfAll(t *testing.T) {
	facts := []*cli.TaskFact{{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: 1, Origin: originOf(thisRun, 1)}}}}
	for i := 1; i <= 250; i++ {
		id := fmt.Sprintf("j%03d", i)
		value := float64(100 - i%90) // the first measurement is the best
		if i == 1 {
			value = 1000
		}
		facts = append(facts,
			&cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, Plans: []uint32{1}, Submitters: mine}}},
			&cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id, Commit: "c", RecordedAt: at(int64(i)), Outcome: &cli.Result_Measure{Measure: &cli.MeasureResult{Metric: &cli.Metric{Name: "x", Direction: maximize}, Value: value}}}}})
	}
	s := project(t, facts...)
	i := ideaOf(s, 1)
	if len(i.CandidateJobIds) != 200 || i.CandidateJobIds[0] != "j051" || i.CandidateJobIds[199] != "j250" || !s.Truncated {
		t.Fatalf("candidates %d, %v, truncated %v", len(i.CandidateJobIds), i.CandidateJobIds[:1], s.Truncated)
	}
	if len(i.Best) != 1 || i.Best[0].BestValue != 1000 || i.Best[0].Ratio != 1 {
		t.Fatalf("the best of all candidates, the baseline itself: %v", i.Best)
	}
}

// A workstream's best is kept within the field's limit even when its ideas together measure more
// objectives than one idea may: each idea's best is already within the limit, but their union over
// the workstream can exceed it, and an over-long best makes the whole SearchState break its rules.
func TestAWorkstreamsBestIsKeptWithinTheFieldLimit(t *testing.T) {
	const ws = 7
	plan := func(number uint32) *cli.TaskFact {
		return &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: number, Workstream: ws, Origin: originOf(thisRun, 1)}}}
	}
	// One job per plan, each measuring five distinct objectives (all stated, ratio 1): ten across the
	// workstream, over the eight the field allows.
	measuring := func(id string, number uint32, names []string) []*cli.TaskFact {
		metrics := make([]*cli.Metric, len(names))
		var obs []*cli.Observation
		for i, name := range names {
			metrics[i] = &cli.Metric{Name: name, Direction: maximize}
			if i > 0 {
				obs = append(obs, &cli.Observation{Name: name, Value: proto.Float64(10)})
			}
		}
		m := &cli.MeasureResult{Metric: metrics[0], Value: 10, Objectives: &cli.Objectives{Metrics: metrics}, Observations: obs}
		return []*cli.TaskFact{
			{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, Plans: []uint32{number}, Submitters: mine}}},
			{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id, Commit: "c", RecordedAt: at(1), Outcome: &cli.Result_Measure{Measure: m}}}},
		}
	}
	facts := []*cli.TaskFact{plan(1), plan(2)}
	facts = append(facts, measuring("jA", 1, []string{"m0", "m1", "m2", "m3", "m4"})...)
	facts = append(facts, measuring("jB", 2, []string{"m5", "m6", "m7", "m8", "m9"})...)
	s := project(t, facts...)
	var view *pb.WorkstreamView
	for _, v := range s.WorkstreamViews {
		if v.Number == ws {
			view = v
		}
	}
	if view == nil {
		t.Fatalf("no view for workstream %d: %v", ws, s.WorkstreamViews)
	}
	if len(view.Best) != 8 {
		t.Fatalf("a workstream best over the field limit is kept at eight, got %d: %v", len(view.Best), view.Best)
	}
}

// When the bytes force a cut, a plan and its idea go together (the lists stay one to one), what is cut
// is the oldest, and no more is cut than the budget asks: what is left is within one plan and one idea
// of the budget.
func TestAPlanAndItsIdeaAreCutTogetherAndNoMoreThanTheBudgetAsks(t *testing.T) {
	property := func(seed int64, size uint8) bool {
		r := rand.New(rand.NewSource(seed))
		n := 150 + int(size)%50
		var facts []*cli.TaskFact
		for i := 1; i <= n; i++ {
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: uint32(i), Body: strings.Repeat("b", 2000+r.Intn(2000)), CreatedAt: at(int64(i)), Origin: originOf(thisRun, 1)}}})
		}
		s := project(t, facts...)
		if len(s.Plans) >= n || len(s.Plans) != len(s.Ideas) || !s.Truncated {
			return false
		}
		largest := 0
		for i, p := range s.Plans {
			if s.Ideas[i].PlanNumber != p.Number || p.Number != uint32(n-i) {
				return false // one to one, and a newest suffix
			}
			largest = max(largest, proto.Size(p))
		}
		return proto.Size(s) > search.MaxStateBytes-largest-2*proto.Size(s.Ideas[0])-16
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 30, Rand: rand.New(rand.NewSource(41))}); err != nil {
		t.Fatal(err)
	}
}
