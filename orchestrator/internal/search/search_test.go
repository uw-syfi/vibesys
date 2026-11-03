package search_test

import (
	"context"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"google.golang.org/protobuf/types/known/timestamppb"
	"testing"
)

func TestSourceAttributionAndDetail(t *testing.T) {
	for _, state := range []pb.JobState{pb.JobState_JOB_STATE_SUCCEEDED, pb.JobState_JOB_STATE_FAILED, pb.JobState_JOB_STATE_CANCELLED, pb.JobState_JOB_STATE_LOST} {
		store := new(search.Store)
		store.Put("r1", search.Fold([]*pb.TaskFact{{Fact: &pb.TaskFact_Job{Job: &pb.Job{Id: "j1", State: state, CancelReason: "unchanged", Submitters: []*pb.Submitter{{RunId: "r1"}, {RunId: "r2"}}}}}}))
		v, _ := store.Get("r1")
		store.Put("r2", v)
		for _, run := range []core.RunID{"r1", "r2"} {
			got, err := (search.Source{Store: store}).Jobs(context.Background(), core.Run{ID: run})
			if err != nil || len(got) != 1 {
				t.Fatalf("%v %v", got, err)
			}
			want := ""
			if state == pb.JobState_JOB_STATE_CANCELLED {
				want = "unchanged"
			}
			if got[0].Detail != want {
				t.Fatal(got)
			}
		}
	}
}

func TestSourceOrdersTellsBySubmissionTime(t *testing.T) {
	store := new(search.Store)
	var facts search.Facts
	for i, id := range []string{"j10", "j2", "j1"} {
		facts = append(facts, &pb.TaskFact{Fact: &pb.TaskFact_Job{Job: &pb.Job{Id: id, State: pb.JobState_JOB_STATE_SUCCEEDED, SubmittedAt: &timestamppb.Timestamp{Seconds: int64(3 - i)}, Submitters: []*pb.Submitter{{RunId: "r"}}}}})
	}
	store.Put("r", search.Fold(facts))
	got, err := (search.Source{Store: store}).Jobs(context.Background(), core.Run{ID: "r"})
	if err != nil || len(got) != 3 || got[0].Job != "j1" || got[1].Job != "j2" || got[2].Job != "j10" {
		t.Fatal(got, err)
	}
}
