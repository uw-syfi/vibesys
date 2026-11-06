package app_test

import (
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
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

// A plan's priority orders its queued jobs before the others, its own priority overrides a job's
// plan, and a low job that waited twice core.PriorityAging goes before a later high one. Each change
// is kept with its reason: a plan comment, or a note on the job.
func TestPriorityOrdersTheQueueAndAgingKeepsALowJobFromStarving(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	ctx := ctxFor(t)
	gate := submit(t, a, "mark", "gate", ";", "hold", "g0", ";", "exit", "0")
	if !w.gates.Reached(ctx, "gate") {
		t.Fatalf("the first job did not start")
	}
	plan, err := a.CreatePlan(ctx, call, "the integration line", "")
	if err != nil {
		t.Fatal(err)
	}
	byPlan := func(name string) core.Job {
		out, err := a.Submit(ctx, app.SubmitRequest{Context: call, Spec: command("mark", name, ";", "hold", "g-"+name, ";", "exit", "0"), Plan: plan.Number})
		if err != nil {
			t.Fatal(err)
		}
		return out.Job
	}
	explore := submit(t, a, "mark", "explore", ";", "hold", "g-explore", ";", "exit", "0")
	gating := byPlan("gating")
	high := core.PriorityHigh
	edited, err := a.EditPlan(ctx, call, plan.Number, app.PlanChange{Priority: &high, Reason: "gates every later plan"})
	if err != nil || edited.Priority != high || len(edited.Comments) != 1 || edited.Comments[0].Agent != call.Agent {
		t.Fatalf("EditPlan = %+v, %v", edited, err)
	}
	if want := core.PriorityChange(core.PriorityNormal, high, "gates every later plan"); edited.Comments[0].Text != want {
		t.Fatalf("the comment is %q, want %q", edited.Comments[0].Text, want)
	}
	low, err := a.SetJobPriority(ctx, call, explore.ID, core.PriorityLow, "exploratory")
	if err != nil || low.Priority != core.PriorityLow || low.Notes[len(low.Notes)-1].Text != core.PriorityChange("", core.PriorityLow, "exploratory") {
		t.Fatalf("SetJobPriority = %+v, %v", low, err)
	}
	if _, err := a.SetJobPriority(ctx, call, explore.ID, core.PriorityHigh, ""); core.AsFailure(err).Kind != core.FailureInvalidInput {
		t.Fatalf("a change of priority with no reason = %v, want invalid input", err)
	}

	// The plan's job goes first though it came later.
	w.gates.Release("g0")
	if !w.gates.Reached(ctx, "gating") {
		t.Fatalf("the high plan's job did not start first")
	}
	if job, _ := a.Job(ctx, scope, explore.ID); job.State != core.JobQueued {
		t.Fatalf("the low job is %s, want it queued behind the high one", job.State)
	}
	// The low job has waited past twice the aging when a new high job comes: it goes first.
	w.clock.Advance(2*core.PriorityAging + time.Minute)
	later := byPlan("later")
	w.gates.Release("g-gating")
	if !w.gates.Reached(ctx, "explore") {
		t.Fatalf("the low job that waited past twice the aging did not start before a later high one")
	}
	if job, _ := a.Job(ctx, scope, later.ID); job.State != core.JobQueued {
		t.Fatalf("the later high job is %s, want it queued", job.State)
	}
	w.gates.Release("g-explore")
	w.gates.Release("g-later")
	for _, id := range []core.JobID{gate.ID, gating.ID, explore.ID, later.ID} {
		awaitState(t, a, id, core.JobSucceeded)
	}
}
