package app_test

import (
	"context"
	"errors"
	"runtime"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// lateCall is a call to Supervisors that opens connections.
type lateCall string

const (
	lateSpawn lateCall = "Spawn"
	lateProbe lateCall = "Probe"
	lateRuns  lateCall = "Runs"
)

// lateSupervisors answers one kind of call only after its context has ended, which is what Close
// does to a daemon, and then answers as it would have a moment before: with the connection it
// opened and no error. That is a call that finished as the cancel was on its way. In the wild
// the scheduler picks whether a call answers before or after the cancel; here the order is built
// in, so the test needs neither the race detector nor luck.
type lateSupervisors struct {
	supervisor.Supervisors
	late     lateCall
	entered  chan struct{}
	backstop <-chan struct{}
	once     sync.Once
}

func newLate(t *testing.T, w *world, late lateCall) (*countedSupervisors, *lateSupervisors) {
	t.Helper()
	l := &lateSupervisors{Supervisors: w.supervisors(), late: late, entered: make(chan struct{}), backstop: ctxFor(t).Done()}
	return &countedSupervisors{Supervisors: l}, l
}

// wait holds the late call until its context ends, and returns a context that has not.
func (s *lateSupervisors) wait(ctx context.Context, call lateCall) context.Context {
	if call != s.late {
		return ctx
	}
	s.once.Do(func() { close(s.entered) })
	select {
	case <-ctx.Done():
	case <-s.backstop:
	}
	return context.WithoutCancel(ctx)
}

func (s *lateSupervisors) Spawn(ctx context.Context, l lifecycle.Launch) (supervisor.Handle, error) {
	return s.Supervisors.Spawn(s.wait(ctx, lateSpawn), l)
}

func (s *lateSupervisors) Runs(ctx context.Context) ([]supervisor.Run, error) {
	return s.Supervisors.Runs(s.wait(ctx, lateRuns))
}

func (s *lateSupervisors) Probe(ctx context.Context, ref lifecycle.Ref) (supervisor.Run, error) {
	return s.Supervisors.Probe(s.wait(ctx, lateProbe), ref)
}

// awaitEntered waits until the late call has been made.
func (s *lateSupervisors) awaitEntered(t *testing.T) {
	t.Helper()
	select {
	case <-s.entered:
	case <-s.backstop:
		t.Fatalf("the daemon never made the %s call", s.late)
	}
}

// awaitEnteredAsTheSlowPeriodPasses is awaitEntered for a call the daemon makes only when the slow
// period has passed, which the test then passes until the daemon makes it.
func (s *lateSupervisors) awaitEnteredAsTheSlowPeriodPasses(t *testing.T, o *outage) {
	t.Helper()
	for {
		select {
		case <-s.entered:
			return
		case <-s.backstop:
			t.Fatalf("the daemon never made the %s call", s.late)
		default:
		}
		o.limits.pass()
		runtime.Gosched()
	}
}

// requireGivenUp fails the test unless the daemon gave up every connection it opened, and opened
// the one the late call returned.
func requireGivenUp(t *testing.T, sups *countedSupervisors) {
	t.Helper()
	opened, closed := sups.opened.Load(), sups.closed.Load()
	if opened == 0 {
		t.Fatal("the late call opened no connection, so the test says nothing")
	}
	if opened != closed {
		t.Fatalf("%d connections opened and %d given up", opened, closed)
	}
}

// P2-3 again, with the order of events built in and not left to the scheduler. A call that opens a
// connection can answer at any moment, including the one at which Close cancels the daemon, and
// the connection it returns then is as much the daemon's to give up as any other. Each subtest
// holds one of the calls that open connections until Close begins, and then lets it return.
func TestAConnectionOpenedAsTheDaemonClosesIsGivenUp(t *testing.T) {
	t.Run("a launch: Spawn answers as Close begins", func(t *testing.T) {
		w, o := newOutage(t)
		sups, late := newLate(t, w, lateSpawn)
		a := o.start(sups)
		submitAs(t, a, callA, "hold", "g", ";", "exit", "0")
		late.awaitEntered(t)
		a.Close()
		requireGivenUp(t, sups)
	})

	t.Run("the sweep of a store that was away: Probe answers as Close begins", func(t *testing.T) {
		w, o := newOutage(t)
		first := o.start(w.supervisors())
		job := submitAs(t, first, callA, "hold", "g", ";", "exit", "0")
		o.awaitIn(t, first, scopeA, job.ID, "running", func(j core.Job) bool { return j.State == core.JobRunning })
		first.Close()
		o.takeAway(repoA)

		sups, late := newLate(t, w, lateProbe)
		a := o.start(sups)
		o.bringBack(repoA)
		late.awaitEnteredAsTheSlowPeriodPasses(t, o)
		a.Close()
		requireGivenUp(t, sups)
	})

	t.Run("a start that is cancelled: Runs answers after the call that started it gave up", func(t *testing.T) {
		w, o := newOutage(t)
		first := o.start(w.supervisors())
		job := submitAs(t, first, callA, "hold", "g", ";", "exit", "0")
		o.awaitIn(t, first, scopeA, job.ID, "running", func(j core.Job) bool { return j.State == core.JobRunning })
		first.Close()
		// The store cannot be read, so the start fails when its own context has ended, and the
		// run it listed is left with no job to adopt it.
		o.takeAway(repoA)

		sups, late := newLate(t, w, lateRuns)
		a := o.build(sups)
		ctx, cancel := context.WithCancel(ctxFor(t))
		defer cancel()
		started := make(chan error, 1)
		go func() { started <- a.Start(ctx) }()
		late.awaitEntered(t)
		cancel()
		select {
		case err := <-started:
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("Start = %v, want it to fail as cancelled", err)
			}
		case <-late.backstop:
			t.Fatal("Start never returned")
		}
		a.Close()
		requireGivenUp(t, sups)
	})
}
