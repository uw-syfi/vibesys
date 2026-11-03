package search_test

import (
	"fmt"
	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"google.golang.org/protobuf/types/known/timestamppb"
	"math/rand"
	"testing"
	"testing/quick"
)

func TestCapKeepsNewestAndTheirFacts(t *testing.T) {
	property := func(size uint16) bool {
		n := int(size % 1100)
		var facts search.Facts
		for i := 0; i < n; i++ {
			id := fmt.Sprint(i)
			facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, SubmittedAt: &timestamppb.Timestamp{Seconds: int64(i)}}}}, &cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id}}}, &cli.TaskFact{Fact: &cli.TaskFact_Trouble{Trouble: &cli.JobTrouble{JobId: id}}}, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{Number: uint32(i + 1)}}}, &cli.TaskFact{Fact: &cli.TaskFact_Promotion{Promotion: &cli.PromotionRecord{Seq: uint64(i + 1)}}}, &cli.TaskFact{Fact: &cli.TaskFact_Judgment{Judgment: &cli.Judgment{Commit: id}}})
		}
		v := search.Fold(facts)
		s := search.Cap(v)
		if len(s.Jobs) != min(n, 500) || len(s.Results) != len(s.Jobs) || len(s.Troubles) != len(s.Jobs) || len(s.Judgments) != min(n, 100) || len(s.Plans) != min(n, 200) || len(s.Promotions) != min(n, 200) || s.Truncated != (n >= 100) {
			return false
		}
		for i, j := range s.Jobs {
			if j.GetSubmittedAt().Seconds != int64(n-1-i) {
				return false
			}
		}
		return len(v.Facts) == len(facts)
	}
	if err := quick.Check(property, &quick.Config{Rand: rand.New(rand.NewSource(1))}); err != nil {
		t.Fatal(err)
	}
}
