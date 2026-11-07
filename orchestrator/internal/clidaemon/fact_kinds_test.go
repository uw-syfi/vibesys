package clidaemon_test

import (
	"strings"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// oneOfEach holds one valid fact of every kind a task's stream carries, by the name of its kind in
// the contract. A kind added to TaskFact fails TestEveryKindOfTaskFactCanBeFollowed until it is
// listed here, which is where its place in the follower's key is decided.
func oneOfEach() map[string]*pb.TaskFact {
	at := timestamppb.New(time.Unix(1, 0))
	return map[string]*pb.TaskFact{
		"job":    {Fact: &pb.TaskFact_Job{Job: &pb.Job{Id: "j1"}}},
		"result": {Fact: &pb.TaskFact_Result{Result: &pb.Result{JobId: "j1"}}},
		"judgment": {Fact: &pb.TaskFact_Judgment{Judgment: &pb.Judgment{
			Commit: strings.Repeat("a", 40), Against: strings.Repeat("b", 40),
			Outcome: &pb.Judgment_Refused{Refused: &pb.Failure{Kind: pb.FailureKind_FAILURE_KIND_UNAVAILABLE, Message: "no"}},
		}}},
		"promotion": {Fact: &pb.TaskFact_Promotion{Promotion: &pb.PromotionRecord{Seq: 1, Promotion: &pb.Promotion{}}}},
		"plan":      {Fact: &pb.TaskFact_Plan{Plan: &pb.Plan{Number: 1}}},
		"head":      {Fact: &pb.TaskFact_Head{Head: &pb.Head{State: &pb.Head_None{None: &pb.NoCurrentResult{}}}}},
		"health":    {Fact: &pb.TaskFact_Health{Health: &pb.StoreHealth{State: &pb.StoreHealth_Live{Live: &pb.Live{}}}}},
		"trouble": {Fact: &pb.TaskFact_Trouble{Trouble: &pb.JobTrouble{
			JobId: "j1", Failure: &pb.Failure{Kind: pb.FailureKind_FAILURE_KIND_UNAVAILABLE, Message: "waiting"},
		}}},
		"bound": {Fact: &pb.TaskFact_Bound{Bound: &pb.ObjectiveBound{
			Seq: 1, Origin: &pb.RecordOrigin{RunId: "r", Agent: "lead", At: at}, Objective: "tok_s",
			Direction: pb.Direction_DIRECTION_MAXIMIZE, Value: 15600, Unit: "tok/s", Note: "derived",
		}}},
	}
}

// Every kind of fact the daemon's task stream can carry has a slot in the follower: a fact the
// follower cannot key ends the stream as lost, and the run's search view goes unreadable for as long
// as the fact is in the store. A declared performance bound was such a fact.
func TestEveryKindOfTaskFactCanBeFollowed(t *testing.T) {
	kinds := (&pb.TaskFact{}).ProtoReflect().Descriptor().Oneofs().ByName("fact").Fields()
	listed := oneOfEach()
	keys := map[string]string{}
	for i := 0; i < kinds.Len(); i++ {
		name := string(kinds.Get(i).Name())
		f, ok := listed[name]
		if !ok {
			t.Fatalf("TaskFact has a kind %q that this test does not know: add a fact of it to oneOfEach, and a key for it to FactKey", name)
		}
		key, err := clidaemon.FactKey(f)
		if err != nil {
			t.Fatalf("a %s fact has no key: %v", name, err)
		}
		if other, dup := keys[key]; dup {
			t.Fatalf("%s and %s facts share the key %q", name, other, key)
		}
		keys[key] = name
	}
	if len(listed) != kinds.Len() {
		t.Fatalf("oneOfEach lists %d kinds, TaskFact has %d", len(listed), kinds.Len())
	}
}

// A stream that carries a kind of every fact is followed whole, and a new version of a bound
// replaces the one before it under the same key only when it has the same place in the log.
func TestAStreamWithEveryKindOfFactIsFollowed(t *testing.T) {
	var s clidaemon.Session
	apply(t, &s, frame(1, &pb.Reset{}))
	var all []*pb.TaskFact
	for _, f := range oneOfEach() {
		all = append(all, f)
	}
	apply(t, &s, frame(2, all))
	apply(t, &s, frame(3, &pb.CaughtUp{}))
	if got := len(s.Facts()); got != len(all) {
		t.Fatalf("held %d facts of %d sent", got, len(all))
	}
	later := oneOfEach()["bound"]
	later.GetBound().Seq = 2
	apply(t, &s, frame(4, []*pb.TaskFact{later}))
	if got := len(s.Facts()); got != len(all)+1 {
		t.Fatalf("a bound with another place in the log is another fact: held %d", got)
	}
}
