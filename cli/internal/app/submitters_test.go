package app_test

import (
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// commandAt is a command job that runs from a given commit, so a plan can link jobs of several.
func commandAt(c core.Commit) core.JobSpec {
	spec := command("exit", "0")
	spec.Commit = c
	return spec
}

func TestASubmissionRecordsTheRunAndAgentThatMadeIt(t *testing.T) {
	r := newResultsRig(t)
	ctx := ctxFor(t)
	caller := func(run core.RunID, agent string) core.CallContext {
		c := call
		c.Run, c.Agent = run, agent
		return c
	}
	var jobs []core.JobID
	for _, c := range []core.CallContext{caller("run-1", "lead"), caller("run-2", "lead"), caller("", "")} {
		out, err := r.app.Submit(ctx, app.SubmitRequest{Context: c, Spec: commandAt(cm(1))})
		if err != nil {
			t.Fatalf("Submit: %v", err)
		}
		jobs = append(jobs, out.Job.ID)
		got := out.Job.Submitters
		if len(got) != 1 || got[0].Run != c.Run || got[0].Agent != c.Agent || got[0].At.IsZero() {
			t.Errorf("submitters of the job a call made = %+v, want the caller %q/%q at the time of the call", got, c.Run, c.Agent)
		}
	}
	for _, id := range jobs {
		awaitFinal(t, r.app, id)
	}
}

// A second run that asks for work an earlier run already did is answered from the finished job.
// The job must name it, or its lead is never told the job finished.
func TestARunAnsweredFromAFinishedJobIsNamedOnIt(t *testing.T) {
	w := newWorld(t)
	w.withTask(passes, scores)
	a := w.newApp()
	ask := func(run core.RunID) core.SubmitOutcome {
		c := call
		c.Run = run
		out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: c, Spec: flowSpec(core.CheckSettings{})})
		if err != nil {
			t.Fatalf("Submit as %s: %v", run, err)
		}
		return out
	}
	first := ask("run-1")
	awaitFinal(t, a, first.Job.ID)
	second := ask("run-2")
	if second.Disposition != core.SubmitReused || second.Job.ID != first.Job.ID {
		t.Fatalf("the second run's submission = %s on %s, want reused on %s", second.Disposition, second.Job.ID, first.Job.ID)
	}
	ask("run-2") // the same caller again adds nothing
	got, err := a.Job(ctxFor(t), scope, first.Job.ID)
	if err != nil {
		t.Fatal(err)
	}
	var runs []core.RunID
	for _, s := range got.Submitters {
		runs = append(runs, s.Run)
	}
	if !reflect.DeepEqual(runs, []core.RunID{"run-1", "run-2"}) || got.State != core.JobSucceeded {
		t.Errorf("job %s in %s names runs %v, want run-1 then run-2, and the job is as it was", got.ID, got.State, runs)
	}
}

func TestAPlanViewGivesTheVerdictOfEachLinkedCommitAgainstTheHead(t *testing.T) {
	r := newResultsRig(t)
	ctx := ctxFor(t)
	r.measured(base, level)
	r.measured(cm(1), win)
	r.measured(cm(2), lose)
	plan, err := r.app.CreatePlan(ctx, call, "tile", "")
	if err != nil {
		t.Fatal(err)
	}
	var ids []core.JobID
	for _, c := range []core.Commit{cm(1), cm(2), cm(3), base} {
		out, err := r.app.Submit(ctx, app.SubmitRequest{Context: call, Spec: commandAt(c), Plan: plan.Number})
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, out.Job.ID)
	}
	for _, id := range ids {
		awaitFinal(t, r.app, id)
	}

	view, err := r.app.ViewPlan(ctx, scope, plan.Number)
	if err != nil {
		t.Fatal(err)
	}
	kinds := map[core.Commit]core.VerdictKind{}
	for c, v := range view.Verdicts {
		kinds[c] = v.Kind
	}
	comparison, err := r.app.Compare(ctx, call, base, base)
	if err != nil {
		t.Fatal(err)
	}
	if got, ok := view.Verdicts[base]; !ok || !reflect.DeepEqual(got, comparison.Verdict) {
		t.Fatalf("head verdict = %+v, want %+v", got, comparison.Verdict)
	}
	// cm(3) is not measured and has none. The head is base, which is measured, and is judged against itself.
	if got := kinds[cm(1)]; got != core.Better {
		t.Errorf("verdict of cm(1) = %q, want better", got)
	}
	if got := kinds[cm(2)]; got != core.Worse {
		t.Errorf("verdict of cm(2) = %q, want worse", got)
	}
	if _, has := kinds[cm(3)]; has {
		t.Errorf("the unmeasured commit has the verdict %q, want none", kinds[cm(3)])
	}
}

func TestAPlanViewJudgesAgainstTheHeadAfterItMoves(t *testing.T) {
	r := newResultsRig(t)
	ctx := ctxFor(t)
	r.measured(base, level)
	r.measured(cm(1), win)
	r.measured(cm(2), lose)
	plan, _ := r.app.CreatePlan(ctx, call, "tile", "")
	out, err := r.app.Submit(ctx, app.SubmitRequest{Context: call, Spec: commandAt(cm(2)), Plan: plan.Number})
	if err != nil {
		t.Fatal(err)
	}
	awaitFinal(t, r.app, out.Job.ID)
	verdictOf := func() (core.Verdict, bool) {
		view, err := r.app.ViewPlan(ctx, scope, plan.Number)
		if err != nil {
			t.Fatal(err)
		}
		v, ok := view.Verdicts[cm(2)]
		return v, ok
	}
	if v, ok := verdictOf(); !ok || v.Kind != core.Worse {
		t.Fatalf("against base, cm(2) = %+v (%v), want worse", v, ok)
	}
	r.promoted(cm(1))
	storetest.SeedMeasurement(t, r.store, storetest.Measurement{Commit: cm(1), Samples: win, At: r.clock.Now()})
	if v, ok := verdictOf(); !ok || v.Kind != core.Worse || v.BaseMean < 115 {
		t.Fatalf("against the promoted cm(1), cm(2) = %+v (%v), want worse than a base near 120", v, ok)
	}
}

func TestAPlanViewWithoutAReadableHeadStillShowsTheJobs(t *testing.T) {
	w := newWorld(t)
	a := w.newApp() // no repository to ask: no result branch
	ctx := ctxFor(t)
	plan, _ := a.CreatePlan(ctx, call, "tile", "")
	out, err := a.Submit(ctx, app.SubmitRequest{Context: call, Spec: command("exit", "0"), Plan: plan.Number})
	if err != nil {
		t.Fatal(err)
	}
	awaitFinal(t, a, out.Job.ID)
	view, err := a.ViewPlan(ctx, scope, plan.Number)
	if err != nil || len(view.Jobs) != 1 || len(view.Verdicts) != 0 {
		t.Fatalf("view = %+v, %v; want the job and no verdicts", view, err)
	}
}
