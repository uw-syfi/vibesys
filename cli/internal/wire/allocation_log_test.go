package wire_test

import (
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

var logAt = time.Date(2026, 11, 10, 9, 8, 7, 654321, time.UTC)

// everyInput is one input of each event the allocation machine takes, with every field set.
func everyInput() []alloc.Input {
	order := alloc.RenewalOrder{Lead: time.Hour, For: 12 * time.Hour, Until: alloc.UntilTime{At: logAt.Add(48 * time.Hour)}}
	events := []alloc.Event{
		alloc.Ask{Run: "run-a", Placement: "p", For: 6 * time.Hour},
		alloc.EndAsk{Run: "run-a", ID: "a1", AfterJobs: true},
		alloc.RenewAsk{Run: "run-a", ID: "a1", Order: order},
		alloc.RenewAsk{Run: "run-a", ID: "a1", Order: alloc.RenewalOrder{Lead: time.Minute, For: time.Hour, Until: alloc.UntilRunEnd{}}},
		alloc.RenewOffAsk{Run: "run-a", ID: "a1"},
		alloc.RunEnded{Run: "run-a"},
		alloc.RunSeen{Run: "run-a", At: logAt},
		alloc.JobSubmitted{Run: "run-a", Job: "ab12-j3", Bound: 90 * time.Minute, Into: "a2", Eligible: []string{"p", "q"}},
		alloc.JobSubmitted{Run: "run-a", Job: "ab12-j4", Bound: time.Minute},
		alloc.JobStarted{Job: "ab12-j3"},
		alloc.JobFinished{Job: "ab12-j3"},
		alloc.Submitted{ID: "a1", Handle: alloc.Handle{Alloc: "a1", Ref: "4242"}},
		alloc.SubmitRefused{ID: "a1", Detail: "no such partition"},
		alloc.Observed{ID: "a1", Seen: alloc.SeenPending{Reason: "Priority", EstimatedStart: logAt.Add(time.Hour)}},
		alloc.Observed{ID: "a1", Seen: alloc.SeenRunning{Node: "node-a", StartedAt: logAt, EndsAt: logAt.Add(time.Hour)}},
		alloc.Observed{ID: "a1", Seen: alloc.SeenEnded{Cause: alloc.EndedByTimeLimit, At: logAt}},
		alloc.Observed{ID: "a1", Seen: alloc.SeenEnded{Cause: alloc.EndedOtherwise}},
		alloc.Unreachable{ID: "a1", Detail: "ssh: timed out"},
		alloc.Released{ID: "a1"},
		alloc.Tick{},
		alloc.DaemonStarted{Config: alloc.Config{Placements: map[string]alloc.Limits{
			"p": {MaxHeld: 2, MaxFor: 24 * time.Hour, Margin: 2 * time.Minute, QueueTimeout: time.Hour, RunIdleEnd: 2 * time.Hour},
			"m": {MaxHeld: 1, MaxFor: time.Hour, Margin: time.Minute, Endless: true},
		}}},
	}
	out := make([]alloc.Input, len(events))
	for i, e := range events {
		out[i] = alloc.Input{At: logAt.Add(time.Duration(i) * time.Second), Event: e}
	}
	return out
}

// Every input of the allocation machine is kept by a message the contract validates, in the text
// the log writes, and read back as the input it was.
func TestEveryAllocationInputSurvivesTheLog(t *testing.T) {
	for _, in := range everyInput() {
		m, err := wire.FromAllocationInput(in)
		if err != nil {
			t.Fatalf("%T: %v", in.Event, err)
		}
		if err := wire.Validate(m); err != nil {
			t.Fatalf("%T: %v", in.Event, err)
		}
		data, err := protojson.Marshal(m)
		if err != nil {
			t.Fatal(err)
		}
		var read cliv1.AllocationInput
		if err := protojson.Unmarshal(data, &read); err != nil {
			t.Fatalf("%T: %v", in.Event, err)
		}
		got, err := wire.ToAllocationInput(&read)
		if err != nil {
			t.Fatalf("%T: %v", in.Event, err)
		}
		if !got.At.Equal(in.At) || !sameEvent(got.Event, in.Event) {
			t.Errorf("%T came back %+v, want %+v", in.Event, got, in)
		}
		again, err := wire.FromAllocationInput(got)
		if err != nil || !proto.Equal(again, m) {
			t.Errorf("%T: the input read back says %v, want %v (%v)", in.Event, again, m, err)
		}
	}
}

// sameEvent compares two events by what the log keeps of them: times by instant.
func sameEvent(a, b alloc.Event) bool {
	ma, errA := wire.FromAllocationInput(alloc.Input{At: logAt, Event: a})
	mb, errB := wire.FromAllocationInput(alloc.Input{At: logAt, Event: b})
	return errA == nil && errB == nil && proto.Equal(ma, mb)
}

func TestAnAllocationInputWithNoEventIsRefused(t *testing.T) {
	if _, err := wire.ToAllocationInput(&cliv1.AllocationInput{}); err == nil {
		t.Fatal("an input with no event was read")
	}
}

func TestARenewalOrderSurvivesTheContract(t *testing.T) {
	for _, order := range []alloc.RenewalOrder{
		{Lead: time.Hour, For: 12 * time.Hour, Until: alloc.UntilRunEnd{}},
		{Lead: time.Minute, For: time.Hour, Until: alloc.UntilTime{At: logAt}},
	} {
		got, err := wire.ToRenewalOrder(wire.FromRenewalOrder(order))
		if err != nil {
			t.Fatal(err)
		}
		if got.Lead != order.Lead || got.For != order.For {
			t.Errorf("order %+v came back %+v", order, got)
		}
		if u, ok := order.Until.(alloc.UntilTime); ok {
			if g, ok := got.Until.(alloc.UntilTime); !ok || !g.At.Equal(u.At) {
				t.Errorf("until %+v came back %+v", order.Until, got.Until)
			}
		} else if _, ok := got.Until.(alloc.UntilRunEnd); !ok {
			t.Errorf("until %+v came back %+v", order.Until, got.Until)
		}
	}
	if _, err := wire.ToRenewalOrder(&cliv1.RenewalOrder{}); err == nil {
		t.Error("an order that says nothing of when it stops was read")
	}
}
