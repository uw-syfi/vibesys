package app_test

import (
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// The history of the superseded tests: the result branch starts at oldHead, and a promotion moves
// it to newHead. stale and staleToo are candidates built on oldHead before the move, and onNewHead is
// built on newHead.
var (
	oldHead   = core.Commit(strings.Repeat("b", 40))
	newHead   = core.Commit(strings.Repeat("c", 40))
	stale     = core.Commit(strings.Repeat("d", 40))
	staleToo  = core.Commit(strings.Repeat("e", 40))
	onNewHead = core.Commit(strings.Repeat("f", 40))
)

// supersededWorld is a world whose task holds every measure in its check until the gate "g" opens,
// with the history above and the result branch at oldHead.
func supersededWorld(t *testing.T) *world {
	t.Helper()
	w := newWorld(t)
	repo := string(call.Scope.Repository())
	w.branches = gitrepo.NewFakeBranches()
	w.branches.AddCommit(repo, oldHead)
	w.branches.AddCommit(repo, newHead, oldHead)
	w.branches.AddCommit(repo, stale, oldHead)
	w.branches.AddCommit(repo, staleToo, oldHead)
	w.branches.AddCommit(repo, onNewHead, newHead)
	w.branches.AddBranch(repo, "vibesys/result/task", oldHead)
	for _, c := range []core.Commit{oldHead, newHead, stale, staleToo, onNewHead} {
		w.tasks.Put(repo, c, tasktest.Scripted("task", []string{"hold", "g", ";", "exit", "0"}, scores))
	}
	return w
}

func submitMeasure(t *testing.T, a *app.App, c core.Commit) core.Job {
	t.Helper()
	spec := core.JobSpec{Commit: c, Settings: core.MeasureSettings{Samples: 2}}
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec, NoPlan: true})
	if err != nil {
		t.Fatalf("Submit measure of %.7s: %v", c, err)
	}
	return out.Job
}

func listed(t *testing.T, a *app.App) []core.Job {
	t.Helper()
	jobs, err := a.Jobs(ctxFor(t), scope, core.JobFilter{})
	if err != nil {
		t.Fatal(err)
	}
	return jobs
}

// A worker measures candidates built on the head of the time, then a promotion moves the head. The
// queued and running measures of those candidates are flagged with the new head, since promote
// refuses a commit that does not contain it; a measure of the new head, of a commit built on it, or
// of a commit the head holds is not. Once the measures end, none is flagged.
func TestMeasuresOfCandidatesThatDoNotContainAMovedHeadAreSuperseded(t *testing.T) {
	w := supersededWorld(t)
	a := w.newApp()
	running := submitMeasure(t, a, stale)
	awaitState(t, a, running.ID, core.JobRunning)
	queued := submitMeasure(t, a, staleToo)
	if got := a.Superseded(ctxFor(t), scope, listed(t, a)); len(got) != 0 {
		t.Fatalf("before the head moved: superseded = %v, want none", got)
	}

	w.branches.AddBranch(string(call.Scope.Repository()), "vibesys/result/task", newHead)
	atHead := submitMeasure(t, a, newHead)
	onHead := submitMeasure(t, a, onNewHead)
	ofBase := submitMeasure(t, a, oldHead)
	jobs := listed(t, a)
	got := a.Superseded(ctxFor(t), scope, jobs)
	want := map[core.JobID]core.Commit{running.ID: newHead, queued.ID: newHead}
	if len(got) != len(want) || got[running.ID] != newHead || got[queued.ID] != newHead {
		t.Fatalf("superseded = %v, want %v (not the new head %s, its child %s or the old head %s)", got, want, atHead.ID, onHead.ID, ofBase.ID)
	}
	for _, j := range jobs {
		if _, flagged := got[j.ID]; flagged && j.State.Final() {
			t.Errorf("job %s flagged although it is %s", j.ID, j.State)
		}
	}

	w.gates.Release("g")
	for _, j := range jobs {
		awaitFinal(t, a, j.ID)
	}
	if got := a.Superseded(ctxFor(t), scope, listed(t, a)); len(got) != 0 {
		t.Fatalf("after every measure ended: superseded = %v, want none", got)
	}
}

// Flagging is advice for a listing and never fails it: a commit the repository lacks, a task with
// no result branch and a task with no store flag nothing.
func TestSupersededFlagsNothingItCannotJudge(t *testing.T) {
	unknown := core.Job{ID: "j-unknown", State: core.JobQueued, Spec: core.JobSpec{Commit: core.Commit(strings.Repeat("9", 40)), Settings: core.MeasureSettings{}}}
	diverged := core.Job{ID: "j-diverged", State: core.JobQueued, Spec: core.JobSpec{Commit: stale, Settings: core.MeasureSettings{}}}

	w := supersededWorld(t)
	a := w.newApp()
	if got := a.Superseded(ctxFor(t), scope, []core.Job{diverged}); len(got) != 0 {
		t.Errorf("a task with no store: superseded = %v", got)
	}
	w.gates.Release("g")
	awaitFinal(t, a, submitMeasure(t, a, oldHead).ID) // makes the store
	w.branches.AddBranch(string(call.Scope.Repository()), "vibesys/result/task", newHead)
	got := a.Superseded(ctxFor(t), scope, []core.Job{unknown, diverged})
	if len(got) != 1 || got[diverged.ID] != newHead {
		t.Errorf("superseded = %v, want only %s, not the job of a commit the repository lacks", got, diverged.ID)
	}

	noBranch := newWorld(t)
	noBranch.branches = gitrepo.NewFakeBranches()
	noBranch.branches.AddCommit(string(call.Scope.Repository()), stale)
	noBranch.withTask(passes, scores)
	b := noBranch.newApp()
	done(t, b, core.CheckSettings{}, false)
	if got := b.Superseded(ctxFor(t), scope, []core.Job{diverged}); len(got) != 0 {
		t.Errorf("a task with no result branch: superseded = %v", got)
	}
}
