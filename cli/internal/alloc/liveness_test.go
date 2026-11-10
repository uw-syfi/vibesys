package alloc_test

import (
	"math/rand/v2"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

const idleEnd = 2 * time.Hour

// livenessWorld is the package's world with run_idle_end set on the cluster's placement.
func livenessWorld(t *testing.T) *world {
	w := newWorld(t)
	w.cfg = changed(gpu, func(l *alloc.Limits) { l.RunIdleEnd = idleEnd })
	return w
}

// A run that holds an allocation and makes no call for run_idle_end is ended at the next tick, as
// alloc end-run ends it: its allocation ends, requested, and its order asks for no more. A call
// keeps it, a call within a minute of the last one changes nothing, and a daemon's start counts as
// one. Before, only alloc end-run ended a run, so a run whose agent died renewed forever.
func TestARunSilentForItsIdleEndIsEnded(t *testing.T) {
	for _, c := range []struct {
		name  string
		calls []time.Duration // after the request
		start time.Duration   // a daemon start at this time, when positive
		tick  time.Duration
		ended bool
	}{
		{"silent, ticked just before", nil, 0, idleEnd - time.Second, false},
		{"silent, ticked at the end", nil, 0, idleEnd, true},
		{"called once, ticked at the old end", []time.Duration{time.Hour}, 0, idleEnd, false},
		{"called once, ticked at the new end", []time.Duration{time.Hour}, 0, time.Hour + idleEnd, true},
		{"called within the granularity", []time.Duration{30 * time.Second}, 0, idleEnd, true},
		{"the daemon started", nil, time.Hour, idleEnd, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			w := livenessWorld(t)
			asked := w.now
			id := w.running(gpu, 10*time.Hour)
			w.accept(alloc.RenewAsk{Run: "run-a", ID: id, Order: alloc.RenewalOrder{Lead: time.Hour, For: 10 * time.Hour, Until: alloc.UntilRunEnd{}}})
			for _, at := range c.calls {
				w.now = asked.Add(at)
				w.do(alloc.RunSeen{Run: "run-a"})
			}
			if c.start > 0 {
				w.now = asked.Add(c.start)
				w.do(alloc.DaemonStarted{Config: w.cfg})
			}
			w.now = asked.Add(c.tick)
			w.do(alloc.Tick{})
			if got := w.s.RunEnded("run-a"); got != c.ended {
				t.Fatalf("the run ended = %v, want %v", got, c.ended)
			}
			if c.ended {
				if end := w.ended(id); end.Reason != alloc.EndRequested {
					t.Fatalf("the allocation ended %s, want requested", end.Reason)
				}
			} else if w.stage(id) != alloc.StageRunning {
				t.Fatalf("the allocation is %s, want running", w.stage(id))
			}
		})
	}
}

// liveRun is a run that renews its allocations for good (--until run-end) on a backend that starts
// each at once, and calls at random gaps until it dies; the daemon ticks every five minutes. It
// returns the time of the run's last call, every successor's request time, and when the run ended.
func liveRun(t *testing.T, gap func() time.Duration, horizon time.Duration) (last time.Time, successors []time.Time, ended time.Time) {
	t.Helper()
	const tick = 5 * time.Minute
	w := livenessWorld(t)
	w.cfg = changed(gpu, func(l *alloc.Limits) { l.RunIdleEnd = idleEnd; l.MaxHeld = 2 })
	start := w.now
	first := w.running(gpu, 3*time.Hour)
	w.accept(alloc.RenewAsk{Run: "run-a", ID: first, Order: alloc.RenewalOrder{Lead: time.Hour, For: 3 * time.Hour, Until: alloc.UntilRunEnd{}}})
	last = w.now
	nextCall := w.now.Add(gap())
	backend := func(actions []alloc.Action) {
		for _, a := range actions {
			if s, ok := a.(alloc.Submit); ok {
				successors = append(successors, w.now)
				w.took(s.Spec.ID)
				w.start(s.Spec.ID)
			}
		}
	}
	for w.now.Sub(start) < horizon {
		w.now = w.now.Add(tick)
		for !nextCall.After(w.now) {
			at := w.now
			w.now = nextCall
			backend(w.do(alloc.RunSeen{Run: "run-a"}))
			last = nextCall
			nextCall = nextCall.Add(gap())
			w.now = at
		}
		backend(w.do(alloc.Tick{}))
		for _, a := range w.s.Allocations() {
			if p, ok := a.Phase.(alloc.Running); ok {
				seen := alloc.Observation(alloc.SeenRunning{})
				if !w.now.Before(p.EndsAt) {
					seen = alloc.SeenEnded{Cause: alloc.EndedByTimeLimit, At: p.EndsAt}
				}
				backend(w.do(alloc.Observed{ID: a.ID, Seen: seen}))
			}
		}
		if ended.IsZero() && w.s.RunEnded("run-a") {
			ended = w.now
		}
	}
	return last, successors, ended
}

// A run that stops calling never has a successor asked for later than its last call, its
// run_idle_end and one tick, and is ended by then; a run that keeps calling, at gaps shorter than
// its run_idle_end less the granularity of a sighting, is never ended for silence and keeps
// renewing.
func TestARunThatStopsCallingStopsRenewingAndOneThatCallsDoesNot(t *testing.T) {
	const tick = 5 * time.Minute
	for seed := range 30 {
		r := rand.New(rand.NewPCG(uint64(seed), 13))
		dies := time.Duration(r.IntN(20)) * time.Hour
		calls := 0
		gap := func() time.Duration {
			calls++
			if time.Duration(calls)*20*time.Minute > dies {
				return 1000 * time.Hour // It died.
			}
			return time.Duration(1+r.IntN(20)) * time.Minute
		}
		last, successors, ended := liveRun(t, gap, 30*time.Hour)
		bound := last.Add(idleEnd + tick)
		for _, at := range successors {
			if at.After(bound) {
				t.Fatalf("seed %d: a successor was asked for at %s, after the last call %s, run_idle_end and a tick", seed, at, last)
			}
		}
		if ended.IsZero() || ended.After(bound) {
			t.Fatalf("seed %d: the run that stopped calling at %s ended at %s, want by %s", seed, last, ended, bound)
		}

		r = rand.New(rand.NewPCG(uint64(seed), 17))
		_, successors, ended = liveRun(t, func() time.Duration {
			return time.Duration(1+r.IntN(int((idleEnd-alloc.SeenGranularity)/time.Minute))) * time.Minute
		}, 30*time.Hour)
		if !ended.IsZero() {
			t.Fatalf("seed %d: a run that kept calling was ended at %s", seed, ended)
		}
		if len(successors) < 5 {
			t.Fatalf("seed %d: a run that kept calling for 30h of 3h allocations renewed %d times", seed, len(successors))
		}
	}
}
