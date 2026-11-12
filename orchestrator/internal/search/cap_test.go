package search_test

import (
	"fmt"
	"testing"
	"testing/quick"

	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/propcheck"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// ours says the run "r" submitted a job.
var ours = []*cli.Submitter{{RunId: "r"}}

// The CLI's store holds the jobs of every run of a task. A run's view holds the jobs it submitted and
// their results and troubles and no others, so a new run starts with none of an earlier run's.
func TestCapKeepsOnlyTheJobsOfTheRun(t *testing.T) {
	property := func(owners []uint8) bool {
		var facts search.Facts
		want := map[string]bool{}
		for i, o := range owners {
			id := fmt.Sprint(i)
			job := &cli.Job{Id: id, SubmittedAt: &timestamppb.Timestamp{Seconds: int64(i)}}
			switch o % 4 {
			case 0:
				job.Submitters = ours
				want[id] = true
			case 1:
				job.Submitters = []*cli.Submitter{{RunId: "other"}}
			case 2:
				job.Submitters = []*cli.Submitter{{RunId: "other"}, {RunId: "r"}}
				want[id] = true
			}
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: job}}, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id}}}, &cli.TaskFact{Fact: &cli.TaskFact_Trouble{Trouble: &cli.JobTrouble{JobId: id}}})
		}
		s := search.Cap(search.Fold(facts), &pb.SearchState{RunId: "r"})
		if len(s.Jobs) != len(want) || len(s.Results) != len(want) || len(s.Troubles) != len(want) {
			return false
		}
		for _, j := range s.Jobs {
			if !want[j.GetId()] {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, propcheck.Config(t, nil)); err != nil {
		t.Fatal(err)
	}
}

func TestCapKeepsNewestAndTheirFacts(t *testing.T) {
	property := func(size uint16) bool {
		n := int(size % 1100)
		var facts search.Facts
		for i := 0; i < n; i++ {
			id := fmt.Sprint(i)
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Submitters: ours, Id: id, SubmittedAt: &timestamppb.Timestamp{Seconds: int64(i)}}}}, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id}}}, &cli.TaskFact{Fact: &cli.TaskFact_Trouble{Trouble: &cli.JobTrouble{JobId: id}}}, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: uint32(i + 1)}}}, &cli.TaskFact{Fact: &cli.TaskFact_Promotion{Promotion: &cli.PromotionRecord{Seq: uint64(i + 1)}}}, &cli.TaskFact{Fact: &cli.TaskFact_Judgment{Judgment: &cli.Judgment{Commit: id}}})
		}
		v := search.Fold(facts)
		s := search.Cap(v, &pb.SearchState{RunId: "r"})
		if len(s.Jobs) != min(n, 500) || len(s.Results) != len(s.Jobs) || len(s.Troubles) != len(s.Jobs) || len(s.Judgments) != min(n, 100) || len(s.Plans) != min(n, 200) || len(s.Promotions) != min(n, 200) || s.Truncated != (n > 100) {
			return false
		}
		for i, j := range s.Jobs {
			if j.GetSubmittedAt().Seconds != int64(n-1-i) {
				return false
			}
		}
		return len(v.Facts) == len(facts)
	}
	if err := quick.Check(property, propcheck.Config(t, nil)); err != nil {
		t.Fatal(err)
	}
}

func TestJudgmentCapKeepsNewestMeasurements(t *testing.T) {
	property := func(extra uint8) bool {
		n := 101 + int(extra)
		var facts search.Facts
		for i := 0; i < n; i++ {
			id := fmt.Sprint(i)
			commit := fmt.Sprintf("%040x", i)
			at := &timestamppb.Timestamp{Seconds: int64(i)}
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Submitters: ours, Id: id, SubmittedAt: at}}}, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id, Commit: commit, RecordedAt: at, Outcome: &cli.Result_Measure{Measure: &cli.MeasureResult{}}}}}, &cli.TaskFact{Fact: &cli.TaskFact_Judgment{Judgment: &cli.Judgment{Commit: commit}}})
		}
		s := search.Cap(search.Fold(facts), &pb.SearchState{RunId: "r"})
		if len(s.Judgments) != 100 || !s.Truncated {
			return false
		}
		for i, j := range s.Judgments {
			if j.Commit != fmt.Sprintf("%040x", n-1-i) {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, propcheck.Config(t, nil)); err != nil {
		t.Fatal(err)
	}
}

func TestExactlyAtEachCapIsNotTruncated(t *testing.T) {
	cases := map[string]struct {
		cap   int
		facts func(int) search.Facts
	}{
		"jobs": {500, func(i int) search.Facts {
			return search.Facts{{Fact: &cli.TaskFact_Job{Job: &cli.Job{Submitters: ours, Id: fmt.Sprint(i)}}}}
		}},
		"results": {500, func(i int) search.Facts {
			id := fmt.Sprint(i)
			return search.Facts{{Fact: &cli.TaskFact_Job{Job: &cli.Job{Submitters: ours, Id: id}}}, {Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id}}}}
		}},
		"troubles": {500, func(i int) search.Facts {
			id := fmt.Sprint(i)
			return search.Facts{{Fact: &cli.TaskFact_Job{Job: &cli.Job{Submitters: ours, Id: id}}}, {Fact: &cli.TaskFact_Trouble{Trouble: &cli.JobTrouble{JobId: id}}}}
		}},
		"judgments": {100, func(i int) search.Facts {
			return search.Facts{{Fact: &cli.TaskFact_Judgment{Judgment: &cli.Judgment{Commit: fmt.Sprint(i)}}}}
		}},
		"plans": {200, func(i int) search.Facts {
			return search.Facts{{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: uint32(i + 1)}}}}
		}},
		"promotions": {200, func(i int) search.Facts {
			return search.Facts{{Fact: &cli.TaskFact_Promotion{Promotion: &cli.PromotionRecord{Seq: uint64(i + 1)}}}}
		}},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			for _, n := range []int{tc.cap - 1, tc.cap, tc.cap + 1} {
				var facts search.Facts
				for i := 0; i < n; i++ {
					facts = append(facts, tc.facts(i)...)
				}
				if s := search.Cap(search.Fold(facts), &pb.SearchState{RunId: "r"}); s.Truncated != (n > tc.cap) {
					t.Fatalf("%s count %d: truncated %v", name, n, s.Truncated)
				}
			}
		})
	}
}
