package storetest

import (
	"context"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// AllocLogWorld is a log under test and how to see it again from a new owner, as a restarted
// daemon does.
type AllocLogWorld struct {
	Log store.AllocLog
	// Reopen returns a log over what the first one kept; the first is not used again. Nil for a log
	// that keeps nothing past its owner.
	Reopen func() store.AllocLog
}

// RunAllocLog checks the contract of store.AllocLog against logs made by newLog.
func RunAllocLog(t *testing.T, newLog func(t *testing.T) AllocLogWorld) {
	t.Helper()
	t.Run("AllocLog", func(t *testing.T) {
		t.Run("an empty log loads nothing", func(t *testing.T) {
			got, err := newLog(t).Log.Load(t.Context())
			if err != nil || len(got) != 0 {
				t.Fatalf("Load of an empty log = %v, %v", got, err)
			}
		})
		t.Run("the inputs come back in the order they were appended", func(t *testing.T) {
			w := newLog(t)
			want := allocInputs(40)
			for _, in := range want {
				if err := w.Log.Append(t.Context(), in); err != nil {
					t.Fatal(err)
				}
			}
			expectInputs(t, w.Log, want)
		})
		t.Run("a log appended to after it was read goes on from there", func(t *testing.T) {
			w := newLog(t)
			want := allocInputs(10)
			for _, in := range want[:6] {
				if err := w.Log.Append(t.Context(), in); err != nil {
					t.Fatal(err)
				}
			}
			expectInputs(t, w.Log, want[:6])
			for _, in := range want[6:] {
				if err := w.Log.Append(t.Context(), in); err != nil {
					t.Fatal(err)
				}
			}
			expectInputs(t, w.Log, want)
		})
		t.Run("a log kept by its owner is the next owner's", func(t *testing.T) {
			w := newLog(t)
			if w.Reopen == nil {
				t.Skip("the log keeps nothing past its owner")
			}
			want := allocInputs(12)
			for _, in := range want[:7] {
				if err := w.Log.Append(t.Context(), in); err != nil {
					t.Fatal(err)
				}
			}
			next := w.Reopen()
			expectInputs(t, next, want[:7])
			// The new owner appends after the old one's last input, without reading the log first.
			for _, in := range want[7:] {
				if err := next.Append(t.Context(), in); err != nil {
					t.Fatal(err)
				}
			}
			expectInputs(t, w.Reopen(), want)
		})
		t.Run("a cancelled call changes nothing", func(t *testing.T) {
			w := newLog(t)
			ctx, cancel := context.WithCancel(t.Context())
			cancel()
			if err := w.Log.Append(ctx, allocInputs(1)[0]); err == nil {
				t.Fatal("Append with a cancelled context succeeded")
			}
			expectInputs(t, w.Log, nil)
		})
	})
}

func expectInputs(t *testing.T, log store.AllocLog, want []alloc.Input) {
	t.Helper()
	got, err := log.Load(t.Context())
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(got) != len(want) {
		t.Fatalf("Load = %d inputs, want %d", len(got), len(want))
	}
	for i := range want {
		if !reflect.DeepEqual(got[i], want[i]) {
			t.Fatalf("input %d = %#v, want %#v", i+1, got[i], want[i])
		}
	}
}

// allocInputs is n inputs, one after another, of every kind of event the allocation machine takes,
// with times in UTC to the nanosecond, as a log keeps them.
func allocInputs(n int) []alloc.Input {
	base := time.Date(2026, 11, 10, 8, 0, 0, 123456789, time.UTC)
	order := alloc.RenewalOrder{Lead: time.Hour, For: 6 * time.Hour, Until: alloc.UntilTime{At: base.Add(100 * time.Hour)}}
	events := []alloc.Event{
		alloc.Ask{Run: "run-a", Placement: "p", For: 6 * time.Hour},
		alloc.Submitted{ID: "a1", Handle: alloc.Handle{Alloc: "a1", Ref: "4242"}},
		alloc.Observed{ID: "a1", Seen: alloc.SeenPending{Reason: "Priority", EstimatedStart: base.Add(time.Hour)}},
		alloc.Observed{ID: "a1", Seen: alloc.SeenRunning{Node: "node-a", StartedAt: base, EndsAt: base.Add(6 * time.Hour)}},
		alloc.JobSubmitted{Run: "run-a", Job: "s1-j1", Bound: time.Hour, Into: "a1", Eligible: []string{"p"}},
		alloc.JobStarted{Job: "s1-j1"},
		alloc.RenewAsk{Run: "run-a", ID: "a1", Order: order},
		alloc.RenewAsk{Run: "run-a", ID: "a1", Order: alloc.RenewalOrder{Lead: time.Minute, For: time.Hour, Until: alloc.UntilRunEnd{}}},
		alloc.RenewOffAsk{Run: "run-a", ID: "a1"},
		alloc.JobFinished{Job: "s1-j1"},
		alloc.EndAsk{Run: "run-a", ID: "a1", AfterJobs: true},
		alloc.Unreachable{ID: "a1", Detail: "timed out"},
		alloc.SubmitRefused{ID: "a2", Detail: "no such partition"},
		alloc.Observed{ID: "a1", Seen: alloc.SeenEnded{Cause: alloc.EndedByTimeLimit, At: base}},
		alloc.Released{ID: "a1"},
		alloc.RunEnded{Run: "run-a"},
		alloc.RunSeen{Run: "run-a"},
		alloc.Tick{},
		alloc.DaemonStarted{Config: alloc.Config{Placements: map[string]alloc.Limits{
			"p": {MaxHeld: 2, MaxFor: 24 * time.Hour, Margin: 2 * time.Minute, QueueTimeout: time.Hour, RunIdleEnd: 2 * time.Hour},
			"m": {MaxHeld: 1, MaxFor: time.Hour, Margin: time.Minute, Endless: true},
		}}},
	}
	out := make([]alloc.Input, n)
	for i := range out {
		out[i] = alloc.Input{At: base.Add(time.Duration(i) * time.Second), Event: events[i%len(events)]}
	}
	return out
}
