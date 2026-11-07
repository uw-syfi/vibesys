package wire_test

import (
	"math/rand/v2"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// legalCheckIn makes a generated check-in hold to the rules that tie its fields together, which a
// random message breaks nearly every time: the facts are there exactly when it is ready, the failure
// exactly when it failed, no turn is unpriced that is not also a turn, a window does not end before it
// starts, and a report only follows a request. The rules of one field are left to the generator.
func legalCheckIn(r *rand.Rand, c *pb.CheckIn) {
	c.State = []pb.CheckInState{pb.CheckInState_CHECK_IN_STATE_PENDING, pb.CheckInState_CHECK_IN_STATE_READY, pb.CheckInState_CHECK_IN_STATE_READY, pb.CheckInState_CHECK_IN_STATE_FAILED}[r.IntN(4)]
	c.Since = timestamppb.New(epoch.Add(time.Duration(r.Int64N(1 << 40))))
	c.At = timestamppb.New(c.Since.AsTime().Add(time.Duration(r.Int64N(1 << 40))))
	if c.AskMessageId == "" {
		c.ReportMessageId = ""
	}
	c.Facts, c.Failure = nil, ""
	switch c.State {
	case pb.CheckInState_CHECK_IN_STATE_READY:
		c.Facts = generated(r, &pb.WindowFacts{})
		legalFacts(r, c.Facts)
	case pb.CheckInState_CHECK_IN_STATE_FAILED:
		c.Failure = "the job log could not be read"
	}
}

func legalFacts(r *rand.Rand, f *pb.WindowFacts) {
	if f.Spend == nil {
		f.Spend = &pb.WindowSpend{}
	}
	f.Spend.UnpricedTurns = min(f.Spend.UnpricedTurns, f.Spend.Turns)
	for _, role := range f.Spend.Roles {
		role.UnpricedTurns = min(role.UnpricedTurns, role.Turns)
	}
	for _, m := range f.Objectives {
		if m.End == nil {
			m.End = generated(r, &pb.BestValue{})
		}
	}
}

// A check-in of every kind comes back from the core types as it went in.
func TestACheckInSurvivesTheCoreTypes(t *testing.T) {
	r := rand.New(rand.NewPCG(31, 32))
	seen := map[pb.CheckInState]int{}
	for range rounds {
		in := generated(r, &pb.CheckIn{})
		legalCheckIn(r, in)
		if wire.Validate(in) != nil {
			continue
		}
		check, err := wire.CheckInFromMessage(in)
		if err != nil {
			t.Fatalf("a legal check-in is refused: %v\n%v", err, in)
		}
		if out := wire.CheckInMessage(check); !proto.Equal(in, out) {
			t.Fatalf("a check-in changed in a round trip\n in: %v\nout: %v", in, out)
		}
		seen[in.State]++
	}
	for _, state := range []pb.CheckInState{pb.CheckInState_CHECK_IN_STATE_PENDING, pb.CheckInState_CHECK_IN_STATE_READY, pb.CheckInState_CHECK_IN_STATE_FAILED} {
		if seen[state] == 0 {
			t.Errorf("no check-in in state %v was accepted", state)
		}
	}
}

// The rules the contract states about a check-in are the ones the core's closed outcomes cannot break:
// whatever the core makes validates.
func TestWhatTheCoreMakesOfACheckInHoldsToTheContract(t *testing.T) {
	best := &core.Best{Value: 2.5, Job: "j", Commit: "c", At: epoch}
	facts := core.WindowFacts{
		Spend: core.Spend{Turns: 2, Unpriced: 1, Roles: []core.RoleSpend{{Role: core.RoleLead, Turns: 2, Unpriced: 1}}},
		Objectives: []core.ObjectiveMove{
			{Metric: "tput", Direction: core.Maximize, Unit: "tok/s", End: best},
			{Metric: "lat", Direction: core.Minimize, Start: best, End: best},
		},
		PlansCreated: []uint32{1}, Reports: []core.MessageID{"m"},
	}
	for _, outcome := range []core.CheckInOutcome{core.CheckInPending{}, core.CheckInReady{Facts: facts}, core.CheckInReady{}, core.CheckInFailed{Detail: "no job log"}} {
		check := core.CheckIn{ID: "c", Run: "r", Viewer: core.Viewer{ID: "v", Label: "laptop"}, Since: epoch, At: epoch.Add(time.Minute), Outcome: outcome, Ask: "m1", Report: "m2"}
		msg := wire.CheckInMessage(check)
		if err := wire.Validate(msg); err != nil {
			t.Fatalf("%T: %v", outcome, err)
		}
		back, err := wire.CheckInFromMessage(msg)
		if err != nil || !checkInsEqual(back, check) {
			t.Fatalf("%T: %+v, %v", outcome, back, err)
		}
	}
}

func checkInsEqual(a, b core.CheckIn) bool {
	return proto.Equal(wire.CheckInMessage(a), wire.CheckInMessage(b))
}
