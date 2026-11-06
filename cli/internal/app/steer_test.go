package app_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A drain lets the job that runs end and starts no queued job, which says it waits on the drain;
// a daemon that restarts drains still, so the queued job stays queued until the daemon resumes.
func TestADrainLetsWhatRunsEndAndStartsNothingAcrossARestart(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	ctx := ctxFor(t)
	running := submit(t, a, "mark", "started", ";", "hold", "g", ";", "exit", "0")
	if !w.gates.Reached(ctx, "started") {
		t.Fatalf("the first job did not start")
	}
	d, err := a.Drain(ctx, "lead", "a new build")
	if err != nil || d.Agent != "lead" || d.Reason != "a new build" || d.Since.IsZero() {
		t.Fatalf("Drain = %+v, %v", d, err)
	}
	queued := submit(t, a, "exit", "0")
	w.gates.Release("g")
	awaitState(t, a, running.ID, core.JobSucceeded)
	if wait := a.Waits(scope, []core.JobID{queued.ID})[queued.ID]; wait.Kind != core.WaitHeld || wait.Detail != d.WaitDetail() {
		t.Fatalf("the queued job waits as %+v, want on the drain", wait)
	}
	if st := a.Status(); st.Drain == nil || st.Queued != 1 || st.Running != 0 {
		t.Fatalf("Status = %+v, want draining with one job queued", st)
	}

	a.Close()
	b := w.newApp()
	if st := b.Status(); st.Drain == nil || *st.Drain != d {
		t.Fatalf("after a restart Status = %+v, want the drain %+v", st, d)
	}
	if job, err := b.Job(ctx, scope, queued.ID); err != nil || job.State != core.JobQueued {
		t.Fatalf("after a restart the queued job is %+v, %v", job, err)
	}
	if wait := b.Waits(scope, []core.JobID{queued.ID})[queued.ID]; wait.Kind != core.WaitHeld || wait.Detail != d.WaitDetail() {
		t.Fatalf("after a restart the queued job waits as %+v, want on the drain", wait)
	}
	if err := b.Resume(ctx); err != nil {
		t.Fatal(err)
	}
	awaitState(t, b, queued.ID, core.JobSucceeded)
	if st := b.Status(); st.Drain != nil {
		t.Fatalf("Status after resuming = %+v, want no drain", st)
	}
	if got, err := w.drains.Load(ctx); got != nil || err != nil {
		t.Fatalf("the record after resuming = %+v, %v; want no drain", got, err)
	}
}

// A held job keeps its place and stands in no one's way: the job after it runs, and once it is
// released it runs next, before a job submitted after the release. A hold of a job that is not
// queued is a conflict and changes nothing.
func TestAHeldJobLetsTheOthersByAndRunsInItsTurnOnceReleased(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	ctx := ctxFor(t)
	first := submit(t, a, "mark", "first", ";", "hold", "g1", ";", "exit", "0")
	if !w.gates.Reached(ctx, "first") {
		t.Fatalf("the first job did not start")
	}
	held := submit(t, a, "mark", "held", ";", "hold", "g2", ";", "exit", "0")
	after := submit(t, a, "mark", "after", ";", "hold", "g3", ";", "exit", "0")
	job, err := a.HoldJob(ctx, call, held.ID, "after the integration check")
	if err != nil || job.Hold == nil || job.Hold.Agent != call.Agent || job.Hold.Reason != "after the integration check" {
		t.Fatalf("HoldJob = %+v, %v", job.Hold, err)
	}
	if wait := a.Waits(scope, []core.JobID{held.ID})[held.ID]; wait.Kind != core.WaitHeld || wait.Detail != job.Hold.WaitDetail() {
		t.Fatalf("the held job waits as %+v", wait)
	}
	if wait := a.Waits(scope, []core.JobID{after.ID})[after.ID]; wait.Kind == core.WaitBehind {
		t.Fatalf("the job after the held one waits behind it: %+v", wait)
	}
	w.gates.Release("g1")
	if !w.gates.Reached(ctx, "after") {
		t.Fatalf("the job after the held one did not start")
	}
	if _, err := a.ReleaseJob(ctx, call, held.ID); err != nil {
		t.Fatal(err)
	}
	later := submit(t, a, "mark", "later", ";", "exit", "0")
	w.gates.Release("g3")
	if !w.gates.Reached(ctx, "held") {
		t.Fatalf("the released job did not start")
	}
	// One slot, and the released job holds it: the later job is still queued.
	if job, err := a.Job(ctx, scope, later.ID); err != nil || job.State != core.JobQueued {
		t.Fatalf("a job submitted after the release is %+v, %v; want it queued behind the released one", job.State, err)
	}
	w.gates.Release("g2")
	awaitState(t, a, held.ID, core.JobSucceeded)
	awaitState(t, a, later.ID, core.JobSucceeded)

	_, err = a.HoldJob(ctx, call, first.ID, "")
	if f := core.AsFailure(err); f.Kind != core.FailureConflict {
		t.Fatalf("a hold of a finished job = %v, want a conflict", err)
	}
}
