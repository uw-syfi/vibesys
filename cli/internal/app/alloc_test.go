package app_test

import (
	"context"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakealloc"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/store/memstore"
)

// allocPeriod is the period of the allocation clock in these tests. No other wait asks for exactly
// it, so a test ticks the clock by saying so (tick) and never by waiting.
const allocPeriod = 7 * time.Second

// allocTimers fires the allocation clock only when a test ticks it, and ends every other short wait
// at once, as clock.Immediate does.
type allocTimers struct {
	mu      sync.Mutex
	credits int
	pending []chan struct{}
}

func (t *allocTimers) Timeout(ctx context.Context, d time.Duration) (<-chan struct{}, func()) {
	if d != allocPeriod {
		return clock.Immediate{Longest: time.Second}.Timeout(ctx, d)
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	fired := make(chan struct{})
	if t.credits > 0 {
		t.credits--
		close(fired)
	} else {
		t.pending = append(t.pending, fired)
	}
	// A wait that is given up is not one a tick fires: a daemon that stopped leaves none behind.
	return fired, func() {
		t.mu.Lock()
		defer t.mu.Unlock()
		t.pending = slices.DeleteFunc(t.pending, func(c chan struct{}) bool { return c == fired })
	}
}

// tick lets the allocation clock fire once: now when it waits, else at its next wait.
func (t *allocTimers) tick() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if len(t.pending) > 0 {
		close(t.pending[0])
		t.pending = t.pending[1:]
		return
	}
	t.credits++
}

// placeDemand lets a job run on the placement "p", claiming one of its slots.
type placeDemand struct{}

func (placeDemand) Options(core.Job) []schedule.Option {
	return []schedule.Option{{Placement: "p", Claims: []schedule.Claim{{Resource: schedule.PlacementOf("p"), Amount: 1}}}}
}

// allocRig is a world whose placement "p" offers standing allocations, over a backend and a log
// that outlive the daemons started on it.
type allocRig struct {
	w      *world
	log    *memstore.AllocLog
	fake   *fakealloc.FakeAllocator
	timers *allocTimers
}

// newAllocRig is a world with a placement of two slots. With backend, its allocations are the
// fake's, as a cluster's are; without, they are slots of the placement itself.
func newAllocRig(t *testing.T, backend bool, o fakealloc.Options) *allocRig {
	t.Helper()
	w := newWorld(t)
	r := &allocRig{w: w, log: &memstore.AllocLog{}, timers: &allocTimers{}}
	r.fake = fakealloc.New(w.clock, o)
	p := app.AllocPlacement{Name: "p", Limits: alloc.Limits{MaxHeld: 2, MaxFor: 2 * time.Hour, Margin: 2 * time.Minute, Endless: !backend}}
	if backend {
		p.Allocator = r.fake
	}
	w.scheduler = app.Scheduler{Resources: []schedule.Resource{{ID: schedule.PlacementOf("p"), Capacity: 2}}, Demand: placeDemand{}}
	w.timers = r.timers
	w.allocations = app.Allocations{Log: r.log, Placements: []app.AllocPlacement{p}, ObservePeriod: allocPeriod}
	return r
}

// restart stops the daemon and starts another on the same disk and backend.
func (r *allocRig) restart(a *app.App) *app.App {
	a.Close()
	return r.w.newApp()
}

func (r *allocRig) request(t *testing.T, a *app.App, d time.Duration) app.AllocationView {
	t.Helper()
	v, err := a.RequestAllocation(ctxFor(t), call.Run, "p", d)
	if err != nil {
		t.Fatalf("RequestAllocation: %v", err)
	}
	return v
}

func (r *allocRig) await(t *testing.T, a *app.App, id alloc.ID, until app.AllocUntilKind) app.AllocationView {
	t.Helper()
	v, outcome, err := a.WaitAllocation(ctxFor(t), id, app.AllocUntil{Kind: until}, time.Hour)
	if err != nil || outcome != app.AllocWaitMet {
		t.Fatalf("waiting for allocation %s to be %s: outcome %s, error %v (it is %s)", id, until, outcome, err, v.Stage())
	}
	return v
}

func submitInto(t *testing.T, a *app.App, into string, script ...string) (core.Job, error) {
	t.Helper()
	spec := command(script...)
	spec.Allocation = into
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec})
	return out.Job, err
}

func jobNamedIn(job core.Job) string {
	return string(lifecycle.StoreIDOf(string(scope.Repository()), string(scope.Task()))) + "-" + string(job.ID)
}

func running(t *testing.T, v app.AllocationView) alloc.Running {
	t.Helper()
	p, ok := v.Phase.(alloc.Running)
	if !ok {
		t.Fatalf("allocation %s is %s, not running", v.ID, v.Stage())
	}
	return p
}

// A job submitted into a running allocation runs there, and ending the allocation once its jobs are
// done leaves nothing of it.
func TestAJobSubmittedIntoARunningAllocationRunsInIt(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{})
	a := r.w.newApp()
	al := r.request(t, a, time.Hour)
	r.await(t, a, al.ID, app.UntilRunning)
	job, err := submitInto(t, a, string(al.ID), "mark", "in-alloc", ";", "hold", "g1", ";", "exit", "0")
	if err != nil {
		t.Fatalf("Submit into %s: %v", al.ID, err)
	}
	if !r.w.gates.Reached(ctxFor(t), "in-alloc") {
		t.Fatal("the job did not start in its allocation")
	}
	cur := running(t, mustAllocation(t, a, al.ID)).Current
	if cur == nil || string(cur.Job) != jobNamedIn(job) {
		t.Fatalf("the allocation runs %+v, want job %s", cur, jobNamedIn(job))
	}
	r.w.gates.Release("g1")
	awaitState(t, a, job.ID, core.JobSucceeded)
	if _, err := a.EndAllocation(ctxFor(t), call.Run, al.ID, true); err != nil {
		t.Fatalf("EndAllocation: %v", err)
	}
	got := r.await(t, a, al.ID, app.UntilEnded)
	if end := got.Phase.(alloc.Ended); end.Reason != alloc.EndRequested {
		t.Fatalf("the allocation ended as %s, want requested", end.Reason)
	}
	// Closing the daemon joins what it started, the release of the allocation among it.
	a.Close()
	if live := r.fake.Live(); len(live) != 0 {
		t.Fatalf("the backend still holds %v after the allocation ended", live)
	}
}

func mustAllocation(t *testing.T, a *app.App, id alloc.ID) app.AllocationView {
	t.Helper()
	v, err := a.Allocation(id)
	if err != nil {
		t.Fatalf("Allocation(%s): %v", id, err)
	}
	return v
}

// A daemon that restarts while an allocation is pending finds it again by its log and its backend,
// asks for nothing more, and sees it start.
func TestARestartWhileAnAllocationIsPendingReadoptsIt(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{Looks: 1})
	a := r.w.newApp()
	al := r.request(t, a, time.Hour)
	if al.Stage() != alloc.StagePending {
		t.Fatalf("the allocation is %s, want pending", al.Stage())
	}
	a = r.restart(a)
	if got := mustAllocation(t, a, al.ID); got.Stage() != alloc.StagePending {
		t.Fatalf("the restarted daemon knows the allocation as %s, want pending", got.Stage())
	}
	r.timers.tick()
	got := r.await(t, a, al.ID, app.UntilRunning)
	if got.Handle == (alloc.Handle{}) {
		t.Fatal("the restarted daemon has no handle of the allocation it saw start")
	}
	if n := len(r.fake.Live()); n != 1 {
		t.Fatalf("the backend holds %d allocations after the restart, want 1: nothing is asked for twice", n)
	}
}

// A job that runs in an allocation keeps running in it across a restart of the daemon.
func TestARestartWhileAJobRunsInAnAllocationReadoptsBoth(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{})
	a := r.w.newApp()
	al := r.request(t, a, time.Hour)
	r.await(t, a, al.ID, app.UntilRunning)
	job, err := submitInto(t, a, string(al.ID), "mark", "in-alloc", ";", "hold", "g1", ";", "exit", "0")
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if !r.w.gates.Reached(ctxFor(t), "in-alloc") {
		t.Fatal("the job did not start")
	}
	a = r.restart(a)
	r.await(t, a, al.ID, app.UntilRunning)
	if cur := running(t, mustAllocation(t, a, al.ID)).Current; cur == nil || string(cur.Job) != jobNamedIn(job) {
		t.Fatalf("after the restart the allocation runs %+v, want job %s", cur, jobNamedIn(job))
	}
	r.w.gates.Release("g1")
	awaitState(t, a, job.ID, core.JobSucceeded)
	if _, err := a.EndAllocation(ctxFor(t), call.Run, al.ID, true); err != nil {
		t.Fatalf("EndAllocation: %v", err)
	}
	r.await(t, a, al.ID, app.UntilEnded)
}

// An allocation the backend holds that this daemon made and no record names is released when the
// daemon starts, and one the user made on the backend is left alone.
func TestAnAllocationNoRecordNamesIsReleased(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{})
	ctx := ctxFor(t)
	orphan, err := r.fake.Request(ctx, alloc.Spec{ID: "a7", Run: "run-9", Placement: "p", For: time.Hour})
	if err != nil {
		t.Fatalf("Request: %v", err)
	}
	users := r.fake.Plant(alloc.Spec{ID: "a8", Run: "run-9", Placement: "p", For: time.Hour})
	a := r.w.newApp()
	// The daemon's own look at what the backend holds is its first, at start.
	r.timers.tick()
	al := r.request(t, a, time.Hour)
	r.await(t, a, al.ID, app.UntilRunning)
	live := r.fake.Live()
	for _, h := range live {
		if h == orphan {
			t.Fatalf("the allocation no record names is still held: %v", live)
		}
	}
	found := false
	for _, h := range live {
		found = found || h == users
	}
	if !found {
		t.Fatalf("the allocation the user made was released: %v", live)
	}
}

// A record whose allocation is gone from the backend ends, as an end the CLI did not ask for.
func TestARecordWhoseAllocationVanishedEndsExternal(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{})
	a := r.w.newApp()
	al := r.request(t, a, time.Hour)
	run := r.await(t, a, al.ID, app.UntilRunning)
	a.Close()
	r.fake.CancelOutside(run.Handle)
	a = r.w.newApp()
	got := r.await(t, a, al.ID, app.UntilEnded)
	if end := got.Phase.(alloc.Ended); end.Reason != alloc.EndExternal {
		t.Fatalf("the allocation ended as %s, want external", end.Reason)
	}
}

// A job submitted to any allocation that none takes runs as a job with no allocation does.
func TestAJobForAnyAllocationFallsBackWhenNothingFits(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{})
	a := r.w.newApp()
	// No allocation at all.
	job, err := submitInto(t, a, core.AllocationAny, "mark", "plain", ";", "exit", "0")
	if err != nil {
		t.Fatalf("Submit --alloc any: %v", err)
	}
	awaitState(t, a, job.ID, core.JobSucceeded)
	// A running allocation too short for the job: the job's bound is its timeout, longer than the allocation.
	al := r.request(t, a, 10*time.Minute)
	r.await(t, a, al.ID, app.UntilRunning)
	job, err = submitInto(t, a, core.AllocationAny, "mark", "plain-2", ";", "exit", "0")
	if err != nil {
		t.Fatalf("Submit --alloc any: %v", err)
	}
	awaitState(t, a, job.ID, core.JobSucceeded)
	if cur := running(t, mustAllocation(t, a, al.ID)).Current; cur != nil {
		t.Fatalf("the allocation ran %+v, but the job did not fit it", cur)
	}
}

// A job submitted into an allocation by name that it does not fit is refused, and the refusal says
// how much time is left and how long the job needs.
func TestAStrictRefusalNamesBothDurations(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{})
	a := r.w.newApp()
	al := r.request(t, a, 10*time.Minute)
	r.await(t, a, al.ID, app.UntilRunning)
	_, err := submitInto(t, a, string(al.ID), "exit", "0")
	f, ok := err.(*core.Failure)
	if !ok || f.Kind != core.FailureConflict {
		t.Fatalf("Submit = %v, want a conflict", err)
	}
	// The command's bound is the daemon's default of 30m; the allocation has just under 10m.
	for _, want := range []string{"30m0s", "left", "9m5"} {
		if !strings.Contains(f.Message, want) {
			t.Errorf("the refusal %q does not say %q", f.Message, want)
		}
	}
	jobs, err := a.Jobs(ctxFor(t), scope, core.JobFilter{})
	if err != nil || len(jobs) != 0 {
		t.Fatalf("a refused submission left jobs %v (%v)", jobs, err)
	}
}

// On a placement whose allocations are slots of its own capacity, a job in an allocation claims
// nothing more: the allocation's slot is its capacity, so with one slot it runs, and a job with no
// allocation waits for the slot until the allocation ends.
func TestALocalSlotIsNotClaimedTwice(t *testing.T) {
	r := newAllocRig(t, false, fakealloc.Options{})
	r.w.scheduler.Resources = []schedule.Resource{{ID: schedule.PlacementOf("p"), Capacity: 1}}
	a := r.w.newApp()
	al := r.request(t, a, time.Hour)
	r.await(t, a, al.ID, app.UntilRunning)
	inside, err := submitInto(t, a, string(al.ID), "mark", "inside", ";", "hold", "g1", ";", "exit", "0")
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if !r.w.gates.Reached(ctxFor(t), "inside") {
		t.Fatal("the job in the allocation did not run on the placement's one slot, which its allocation holds")
	}
	outside := submit(t, a, "mark", "outside", ";", "exit", "0")
	if r.w.gates.Marked("outside") {
		t.Fatal("a job with no allocation ran while the allocation held the placement's only slot")
	}
	r.w.gates.Release("g1")
	awaitState(t, a, inside.ID, core.JobSucceeded)
	if _, err := a.EndAllocation(ctxFor(t), call.Run, al.ID, false); err != nil {
		t.Fatalf("EndAllocation: %v", err)
	}
	awaitState(t, a, outside.ID, core.JobSucceeded)
}
