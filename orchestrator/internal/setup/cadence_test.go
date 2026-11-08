package setup_test

import (
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

// rig drives Next the way the daemon's loop does, on a clock value it owns.
type rig struct {
	t        *testing.T
	s        setup.Schedule
	now      time.Time
	inFlight bool
	starts   []time.Time
}

func newRig(t *testing.T) *rig { return &rig{t: t, now: epoch} }

func (g *rig) step(ev setup.Events) setup.Action {
	g.t.Helper()
	if ev.Finished != setup.ReadNone {
		if !g.inFlight {
			g.t.Fatal("harness finished a read that was not in flight")
		}
		g.inFlight = false
	}
	var a setup.Action
	g.s, a = setup.Next(g.s, g.now, ev)
	if a.Read {
		if g.inFlight {
			g.t.Fatalf("a second read started at %v", g.now)
		}
		if n := len(g.starts); n > 0 && g.now.Sub(g.starts[n-1]) < setup.MinGap {
			g.t.Fatalf("reads started %v apart", g.now.Sub(g.starts[n-1]))
		}
		g.inFlight = true
		g.starts = append(g.starts, g.now)
	}
	return a
}

func (g *rig) advance(d time.Duration) { g.now = g.now.Add(d) }

func TestFiftyTriggersDuringAReadMakeExactlyOneMoreRead(t *testing.T) {
	g := newRig(t)
	if !g.step(setup.Events{}).Read {
		t.Fatal("no first read")
	}
	for range 50 {
		g.advance(10 * time.Millisecond)
		if g.step(setup.Events{Triggered: true}).Read {
			t.Fatal("a trigger started a read while one was in flight")
		}
	}
	g.advance(20 * time.Millisecond)
	a := g.step(setup.Events{Finished: setup.ReadChanged})
	wantAt := g.starts[0].Add(setup.MinGap)
	if a.Read || !a.Until.Equal(wantAt) {
		t.Fatalf("after the read: %+v, want a wait until %v", a, wantAt)
	}
	g.now = wantAt
	if !g.step(setup.Events{}).Read {
		t.Fatal("no extra read once the gap passed")
	}
	g.advance(time.Millisecond)
	g.step(setup.Events{Finished: setup.ReadUnchanged})
	if a := g.step(setup.Events{}); a.Read {
		t.Fatalf("a second extra read: %+v", a)
	}
	if len(g.starts) != 2 {
		t.Fatalf("%d reads", len(g.starts))
	}
}

func TestAnEditWithNoTriggerIsSeenWithinIdle(t *testing.T) {
	g := newRig(t)
	g.step(setup.Events{})
	g.advance(100 * time.Millisecond)
	a := g.step(setup.Events{Finished: setup.ReadChanged})
	if a.Read || !a.Until.Equal(g.now.Add(setup.Idle)) {
		t.Fatalf("%+v", a)
	}
	g.now = a.Until.Add(-time.Nanosecond)
	if g.step(setup.Events{}).Read {
		t.Fatal("read before Idle")
	}
	g.advance(time.Nanosecond)
	if !g.step(setup.Events{}).Read {
		t.Fatal("no read at Idle")
	}
}

func TestIdleBacksOffToIdleMaxAndAnyTriggerResetsIt(t *testing.T) {
	g := newRig(t)
	g.step(setup.Events{})
	var gaps []time.Duration
	for range 8 {
		a := g.step(setup.Events{Finished: setup.ReadUnchanged})
		gaps = append(gaps, a.Until.Sub(g.now))
		g.now = a.Until
		g.step(setup.Events{})
	}
	if gaps[0] != setup.Idle || gaps[len(gaps)-1] != setup.IdleMax {
		t.Fatalf("gaps %v", gaps)
	}
	for i := 1; i < len(gaps); i++ {
		if gaps[i] < gaps[i-1] || gaps[i] > setup.IdleMax {
			t.Fatalf("gaps %v", gaps)
		}
	}
	g.advance(10 * time.Millisecond)
	g.step(setup.Events{Triggered: true})
	g.advance(setup.MinGap)
	if !g.step(setup.Events{Finished: setup.ReadUnchanged}).Read {
		t.Fatal("the trigger made no read")
	}
	g.advance(time.Millisecond)
	a := g.step(setup.Events{Finished: setup.ReadUnchanged})
	if got := a.Until.Sub(g.now); got > 2*setup.Idle {
		t.Fatalf("after a trigger the wait is %v", got)
	}
}

func TestFailuresBackOffAndASuccessResets(t *testing.T) {
	g := newRig(t)
	g.step(setup.Events{})
	var gaps []time.Duration
	for range 8 {
		a := g.step(setup.Events{Finished: setup.ReadFailed})
		gaps = append(gaps, a.Until.Sub(g.now))
		g.now = a.Until
		g.step(setup.Events{})
	}
	if gaps[0] != setup.FailureMin || gaps[len(gaps)-1] != setup.FailureMax {
		t.Fatalf("gaps %v", gaps)
	}
	// A trigger does not hurry a failing read past its wait.
	g.advance(10 * time.Millisecond)
	a := g.step(setup.Events{Finished: setup.ReadFailed, Triggered: true})
	if a.Read || a.Until.Sub(g.now) != setup.FailureMax {
		t.Fatalf("%+v", a)
	}
	g.now = a.Until
	g.step(setup.Events{})
	a = g.step(setup.Events{Finished: setup.ReadChanged})
	if got := a.Until.Sub(g.now); got != setup.Idle {
		t.Fatalf("after a success the wait is %v", got)
	}
}

// Over any sequence of triggers, finished reads and time passing: never two reads in flight, never
// two starts inside MinGap (rig checks both at every start), from any state a read comes within the longest wait.
func TestCadenceHoldsItsRulesOverRandomSequences(t *testing.T) {
	longest := max(setup.IdleMax, setup.FailureMax, setup.MinGap)
	eachSeed(t, func(t *testing.T, seed uint64) {
		r := newRand(seed)
		g := newRig(t)
		for range 200 {
			ev := setup.Events{}
			switch r.IntN(4) {
			case 0:
				ev.Triggered = true
			case 1:
				if g.inFlight {
					ev.Finished = pick(r, setup.ReadChanged, setup.ReadUnchanged, setup.ReadFailed)
				}
			}
			if r.IntN(2) == 0 {
				g.advance(time.Duration(r.IntN(3000)) * time.Millisecond)
			}
			a := g.step(ev)
			if !a.Read && !g.inFlight && !a.Until.After(g.now) {
				t.Fatalf("waiting until %v at %v", a.Until, g.now)
			}
			if !a.Read && !g.inFlight && a.Until.Sub(g.now) > longest {
				t.Fatalf("a wait of %v", a.Until.Sub(g.now))
			}
		}
		var a setup.Action
		for range 3 {
			ev := setup.Events{}
			if g.inFlight {
				ev.Finished = setup.ReadUnchanged
			}
			if a = g.step(ev); !a.Read {
				break
			}
		}
		if a.Read || a.Until.IsZero() {
			t.Fatalf("no wait after the reads settle: %+v", a)
		}
		g.now = a.Until
		if !g.step(setup.Events{}).Read {
			t.Fatalf("no read at the end of the wait %v", a.Until)
		}
	})
}
