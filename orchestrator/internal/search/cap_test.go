package search_test

import (
	"context"
	"fmt"
	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"testing"
	"time"
)

func TestCapsKeepNewestJobsTheirResultsAndFullSource(t *testing.T) {
	for _, n := range []int{499, 500, 501, 750} {
		var facts search.Facts
		for i := range n {
			id := fmt.Sprint(i)
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, State: cli.JobState_JOB_STATE_SUCCEEDED, Submitters: []*cli.Submitter{{RunId: "r"}}, SubmittedAt: timestamppb.New(time.Unix(int64(i), 0))}}}, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id}}})
		}
		view := search.Fold(facts)
		store := new(search.Store)
		store.Put("r", view)
		updates, err := (search.Source{Store: store}).Jobs(context.Background(), core.Run{ID: "r"})
		if err != nil || len(updates) != n {
			t.Fatalf("notification source capped %d jobs to %d: %v", n, len(updates), err)
		}
		capped := search.Cap(view)
		twice := search.Cap(capped)
		if twice.Truncated != capped.Truncated || twice.UnattributedJobs != capped.UnattributedJobs || len(twice.Facts) != len(capped.Facts) {
			t.Fatal("cap is not idempotent")
		}
		for i := range twice.Facts {
			if !proto.Equal(twice.Facts[i], capped.Facts[i]) {
				t.Fatal("cap changed retained facts")
			}
		}
		jobs, results := 0, 0
		for _, f := range capped.Facts {
			if j := f.GetJob(); j != nil {
				jobs++
				if j.SubmittedAt.Seconds < int64(n-500) {
					t.Fatal("kept old job")
				}
			}
			if f.GetResult() != nil {
				results++
			}
		}
		if jobs != min(n, 500) || results != jobs || capped.Truncated != (n > 500) || len(view.Facts) != 2*n {
			t.Fatalf("n=%d jobs=%d results=%d truncated=%v", n, jobs, results, capped.Truncated)
		}
	}
}
func TestStoreOwnsSnapshotsAndAdvancesOnlyOnChange(t *testing.T) {
	store := new(search.Store)
	v := search.Fold(search.Facts{{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1"}}}})
	store.Put("r", v)
	_, a := store.Get("r")
	store.Put("r", v)
	got, b := store.Get("r")
	if a != b {
		t.Fatal("unchanged view advanced revision")
	}
	v.Facts[0].GetJob().Id = "mutated"
	got.Facts[0].GetJob().Id = "also mutated"
	got, _ = store.Get("r")
	if got.Facts[0].GetJob().Id != "j1" {
		t.Fatal("aliased snapshot")
	}
	got.Reason = search.StreamLost
	got.Detail = "cut"
	store.Put("r", got)
	_, c := store.Get("r")
	if c <= b {
		t.Fatal("health change did not advance revision")
	}
}

func TestOtherDisplayCapsAndStoreHealth(t *testing.T) {
	var facts search.Facts
	for n := range 201 {
		facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: uint32(n + 1)}}}, &cli.TaskFact{Fact: &cli.TaskFact_Promotion{Promotion: &cli.PromotionRecord{Seq: uint64(n + 1)}}}, &cli.TaskFact{Fact: &cli.TaskFact_Judgment{Judgment: &cli.Judgment{Commit: fmt.Sprint(n)}}})
	}
	facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Health{Health: &cli.StoreHealth{State: &cli.StoreHealth_Away{Away: &cli.Away{Reason: cli.AwayReason_AWAY_REASON_OWNED_ELSEWHERE, Failure: &cli.Failure{Message: "owned"}}}}}})
	v := search.Cap(search.Fold(facts))
	plans, promotions, judgments := 0, 0, 0
	for _, f := range v.Facts {
		if p := f.GetPlan(); p != nil {
			plans++
			if p.Number == 1 {
				t.Fatal("old plan")
			}
		}
		if p := f.GetPromotion(); p != nil {
			promotions++
			if p.Seq == 1 {
				t.Fatal("old promotion")
			}
		}
		if f.GetJudgment() != nil {
			judgments++
		}
	}
	if plans != 200 || promotions != 200 || judgments != 100 || !v.Truncated || v.Reason != search.StoreOwnedElsewhere || v.Detail != "owned" {
		t.Fatal(v)
	}
}
