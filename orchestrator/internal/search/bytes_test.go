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
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestCapBoundsBytesAndDropsOldest(t *testing.T) {
	property := func(lengths []uint16) bool {
		var facts search.Facts
		for i, n := range lengths {
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: uint32(i + 1), Body: strings.Repeat("x", int(n)*8), CreatedAt: &timestamppb.Timestamp{Seconds: int64(i)}}}})
		}
		v := search.Fold(facts)
		s := search.Cap(v, &pb.SearchState{RunId: "r", Revision: 1, Health: &pb.SearchHealth{State: &pb.SearchHealth_Unreadable{Unreadable: &pb.Unreadable{Reason: pb.UnreadableReason_UNREADABLE_REASON_STREAM_LOST, Since: &timestamppb.Timestamp{}, Detail: search.Detail(strings.Repeat("界", 5000))}}}})
		if proto.Size(s) > search.MaxStateBytes {
			return false
		}
		// Survivors are always a newest suffix, even when a single entry is oversized.
		for i, p := range s.Plans {
			if p.Number != uint32(len(lengths)-i) {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 50, Rand: rand.New(rand.NewSource(12))}); err != nil {
		t.Fatal(err)
	}
	// A single huge singleton must not defeat the bound after lists are removed.
	s := search.Cap(search.Fold(search.Facts{{Fact: &cli.TaskFact_Head{Head: &cli.Head{State: &cli.Head_Current{Current: &cli.CurrentResult{Head: strings.Repeat("a", search.MaxStateBytes*2)}}}}}}), &pb.SearchState{RunId: "r"})
	if proto.Size(s) > search.MaxStateBytes || !s.Truncated || s.Head != nil {
		t.Fatal("oversized head retained")
	}
}

func TestCapFullListsWithLongFields(t *testing.T) {
	var facts search.Facts
	for i := 0; i < 500; i++ {
		id := fmt.Sprint(i)
		facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, CancelReason: strings.Repeat("x", 2048), SubmittedAt: &timestamppb.Timestamp{Seconds: int64(i)}}}}, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id, Task: strings.Repeat("y", 2048), RecordedAt: &timestamppb.Timestamp{Seconds: int64(i)}}}})
	}
	s := search.Cap(search.Fold(facts), &pb.SearchState{RunId: "r", Revision: 1})
	if proto.Size(s) > search.MaxStateBytes || !s.Truncated {
		t.Fatal(proto.Size(s), s.Truncated)
	}
}
