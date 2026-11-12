package app_test

import (
	"context"
	"runtime"
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
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
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
	// The backend's queue stays shut until the test opens it: how many times either daemon looked at
	// the allocation around the restart is the scheduler's to say, and must not decide the outcome.
	r := newAllocRig(t, true, fakealloc.Options{Queued: true})
	a := r.w.newApp()
	al := r.request(t, a, time.Hour)
	if al.Stage() != alloc.StagePending {
		t.Fatalf("the allocation is %s, want pending", al.Stage())
	}
	a = r.restart(a)
	if got := mustAllocation(t, a, al.ID); got.Stage() != alloc.StagePending {
		t.Fatalf("the restarted daemon knows the allocation as %s, want pending", got.Stage())
	}
	r.fake.OpenQueue()
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
	// The command's bound is the daemon's default of 30m; the allocation has 10m, said in whole seconds.
	for _, want := range []string{"30m0s", "left", "10m0s"} {
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

// hangingLog is an allocation log whose reads end only with their context, as a log on a file
// system that stopped answering does.
type hangingLog struct{ memstore.AllocLog }

func (*hangingLog) Load(ctx context.Context) ([]alloc.Input, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}

// A daemon whose allocation log never answers fails to start, as one whose stores never do fails the
// call: the read of the log is bounded, and the start says what it could not read. Before, the
// start waited for the log for as long as the file system hung.
func TestADaemonStartFailsWhenItsAllocationLogNeverAnswers(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{})
	// Every wait of up to two hours ends at once, the bound of a call among them.
	r.w.timers = clock.Immediate{Longest: 2 * time.Hour}
	r.w.allocations.Log = &hangingLog{}
	a, err := r.w.tryNewApp()
	if err == nil {
		t.Fatal("the daemon started over an allocation log that never answers")
	}
	if !strings.Contains(err.Error(), "allocation log") {
		t.Fatalf("Start failed with %q, want it to name the allocation log", err)
	}
	a.Close()
}

// A pending allocation whose backend changes only its display words at every look (Slurm's estimate
// of when it starts) adds nothing to the log: the words are kept in memory and shown, and only a
// change of what the allocation is is logged. Before, every look whose estimate moved appended an
// input, so a daemon that read its whole log at start had a record for every five seconds of every
// wait.
func TestAMovingEstimateOfAPendingAllocationIsNotLogged(t *testing.T) {
	const looks = 40
	r := newAllocRig(t, true, fakealloc.Options{Looks: looks + 1, MovingEstimate: true})
	a := r.w.newApp()
	al := r.request(t, a, time.Hour)
	// movedFrom waits until the allocation shows an estimate other than last, and returns it.
	movedFrom := func(last time.Time) time.Time {
		t.Helper()
		ctx := ctxFor(t)
		for {
			if p, ok := mustAllocation(t, a, al.ID).Phase.(alloc.Pending); ok && !p.EstimatedStart.IsZero() && !p.EstimatedStart.Equal(last) {
				return p.EstimatedStart
			}
			// A change of the allocation wakes the wait, which is bounded only against a hang.
			if _, _, err := a.WaitAllocation(ctx, al.ID, app.AllocUntil{Kind: app.UntilEnded}, time.Millisecond); err != nil {
				t.Fatalf("waiting for the estimate to move from %s: %v", last, err)
			}
		}
	}
	first := movedFrom(time.Time{})
	before, err := r.log.Load(ctxFor(t))
	if err != nil {
		t.Fatal(err)
	}
	last := first
	for range looks - 1 {
		r.timers.tick()
		last = movedFrom(last)
	}
	if want := first.Add((looks - 1) * time.Minute); !last.Equal(want) {
		t.Fatalf("the last estimate shown is %s, want %s: a look was missed", last, want)
	}
	after, err := r.log.Load(ctxFor(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(after) != len(before) {
		t.Fatalf("%d looks that moved only the estimate appended %d inputs, want none: %+v", looks-1, len(after)-len(before), after[len(before):])
	}
	// The restarted daemon has the allocation as the log has it, and its next look says the words again.
	a = r.restart(a)
	if got := mustAllocation(t, a, al.ID); got.Stage() != alloc.StagePending || got.Handle == (alloc.Handle{}) {
		t.Fatalf("after a restart the allocation is %+v, want it pending with its handle", got)
	}
}

// A second allocation at the backend for an ID whose record has another handle, such as a request
// that timed out and landed after it was asked again, is released when the daemon adopts what the
// backend holds; the record keeps its own, which goes on running. Before, adoption ignored it, and it
// held its capacity for its whole time limit.
func TestASecondAllocationForARecordedIDIsReleased(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{})
	a := r.w.newApp()
	al := r.request(t, a, time.Hour)
	run := r.await(t, a, al.ID, app.UntilRunning)
	dup := r.fake.Duplicate(alloc.Spec{ID: al.ID, Run: call.Run, Placement: "p", For: time.Hour})
	a = r.restart(a)
	ctx := ctxFor(t)
	for slices.Contains(r.fake.Live(), dup) {
		r.timers.tick()
		// Each look is the daemon's; the wait only lets it run, bounded against a hang.
		if _, _, err := a.WaitAllocation(ctx, al.ID, app.AllocUntil{Kind: app.UntilEnded}, time.Millisecond); err != nil {
			t.Fatalf("the duplicate %s was never released: %v", dup.Ref, err)
		}
	}
	got := mustAllocation(t, a, al.ID)
	if got.Handle != run.Handle || got.Stage() != alloc.StageRunning || !slices.Contains(r.fake.Live(), run.Handle) {
		t.Fatalf("after the duplicate's release the allocation is %+v (live %v), want it running on %s", got, r.fake.Live(), run.Handle.Ref)
	}
}

// A daemon restarted with a config whose placement lost its allocations section ends that
// placement's allocations, external, and releases them at the backend, and logs that it did; a
// later start folds the log to the same ends. Before, the records were replayed under the new
// config and the backend's allocations were left to run their time limit.
func TestARemovedAllocationsSectionReleasesItsAllocationsAtStart(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{})
	a := r.w.newApp()
	al := r.request(t, a, time.Hour)
	r.await(t, a, al.ID, app.UntilRunning)
	a.Close()
	former := r.w.allocations.Placements[0]
	r.w.allocations.Placements, r.w.allocations.Former = nil, []app.AllocPlacement{{Name: former.Name, Allocator: r.fake}}
	a = r.w.newApp()
	got := r.await(t, a, al.ID, app.UntilEnded)
	if end := got.Phase.(alloc.Ended); end.Reason != alloc.EndExternal {
		t.Fatalf("the allocation ended as %s, want external", end.Reason)
	}
	// Its release is a step of the daemon's own, bounded against a hang.
	ctx := ctxFor(t)
	for len(r.fake.Live()) > 0 {
		if ctx.Err() != nil {
			t.Fatalf("the backend still holds %v", r.fake.Live())
		}
		r.timers.tick()
		runtime.Gosched()
	}
	if _, err := a.RequestAllocation(ctx, call.Run, former.Name, time.Hour); err == nil {
		t.Fatal("a placement with no allocations section took a request")
	}
	a = r.restart(a)
	if got := mustAllocation(t, a, al.ID); got.Stage() != alloc.StageEnded {
		t.Fatalf("after another restart the allocation is %s, want ended", got.Stage())
	}
}

// launches records the launch of every job the daemon starts.
type launches struct {
	supervisor.Supervisors
	mu   *sync.Mutex
	seen *[]lifecycle.Launch
}

func (l launches) Spawn(ctx context.Context, launch lifecycle.Launch) (supervisor.Handle, error) {
	l.mu.Lock()
	*l.seen = append(*l.seen, launch)
	l.mu.Unlock()
	return l.Supervisors.Spawn(ctx, launch)
}

// The reviewer's case: an allocation with 60m left and a fit margin of 2m takes a job of a 55m
// bound, and the job's launch carries the latest time its first container may enter it, 3m on,
// so that a preparation of 10m (an image build) does not start a job the allocation's end would
// kill. Before, the bound was counted from the launch, and the job was killed at the time limit.
func TestAJobInAnAllocationLaunchesWithTheWindowItMustEnterIn(t *testing.T) {
	r := newAllocRig(t, true, fakealloc.Options{})
	var mu sync.Mutex
	var seen []lifecycle.Launch
	r.w.wrap = func(s supervisor.Supervisors) supervisor.Supervisors {
		return launches{Supervisors: s, mu: &mu, seen: &seen}
	}
	a := r.w.newApp()
	al := r.request(t, a, time.Hour)
	ends := running(t, r.await(t, a, al.ID, app.UntilRunning)).EndsAt
	spec := command("mark", "in", ";", "exit", "0")
	spec.Allocation = string(al.ID)
	spec.Settings = core.CommandSettings{Image: core.ImageHarness, Argv: spec.Settings.(core.CommandSettings).Argv, Timeout: 55 * time.Minute}
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	awaitState(t, a, out.Job.ID, core.JobSucceeded)
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 1 {
		t.Fatalf("%d launches, want the job's", len(seen))
	}
	// The latest entry is the allocation's end less the margin and the bound.
	if got, want := seen[0].AllocationEnterBy, ends.Add(-2*time.Minute-55*time.Minute); !got.Equal(want) {
		t.Fatalf("the launch's latest entry is %v, want %v (60m left, 2m margin, 55m bound)", got, want)
	}
}
