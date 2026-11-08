package app_test

import (
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// The measurements of a reading are the base commit's, under the version of the draft: a
// measurement of another commit, or of the task as it was before an edit, is not among them.
func TestReadSetupHoldsOnlyTheBasesMeasurementsAtTheDraftsVersion(t *testing.T) {
	w := newWorld(t)
	w.branches = gitrepo.NewFakeBranches()
	repo := string(call.Scope.Repository())
	other := core.Commit(strings.Repeat("cd", 20))
	w.git.AddRepo(repo, nil)
	w.branches.AddCommit(repo, commit)
	w.branches.AddCommit(repo, other, commit)
	w.branches.SetHead(repo, commit)
	w.withTask(passes, scores)
	w.tasks.Put(repo, other, tasktest.Scripted("task", passes, scores))
	files := task.NewMemoryCardFiles()
	files.PutWorkingTree(repo, tasktest.Scripted("task", passes, scores))
	a := w.newApp()
	read := func() app.SetupRead {
		t.Helper()
		got, err := a.ReadSetup(ctxFor(t), app.Caller{Directory: repo, Task: "task"}, files)
		if err != nil {
			t.Fatal(err)
		}
		return got
	}
	if got := read(); len(got.Measurements) != 0 {
		t.Fatalf("before any job: %+v", got.Measurements)
	}
	atBase, _ := done(t, a, core.MeasureSettings{Samples: 2}, false)
	if atBase.State != core.JobSucceeded {
		t.Fatalf("measure = %s", describe(atBase))
	}
	spec := core.JobSpec{Commit: other, Settings: core.MeasureSettings{Samples: 2}}
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec, NoPlan: noPlan(spec)})
	if err != nil || awaitFinal(t, a, out.Job.ID).State != core.JobSucceeded {
		t.Fatalf("measure of the other commit: %v", err)
	}
	got := read()
	if len(got.Measurements) != 1 || got.Measurements[0].Commit != commit || got.Measurements[0].TaskVersion != got.Draft.Version || len(got.Measurements[0].Outcome.Samples) != 2 {
		t.Fatalf("measurements = %+v, want the base's, at version %q", got.Measurements, got.Draft.Version)
	}
	if len(got.Jobs) != 2 || got.Jobs[0].Relation != app.RelationDescendant || got.Jobs[1].Relation != app.RelationBase {
		t.Errorf("jobs = %+v, want the other commit's (a descendant) and then the base's", got.Jobs)
	}
	edited := tasktest.Scripted("task", passes, scores)
	edited[taskDir+"extra"] = task.File{Data: []byte("x")}
	files.PutWorkingTree(repo, edited)
	if got := read(); len(got.Measurements) != 0 {
		t.Errorf("after an edit to the task: %+v", got.Measurements)
	}
}
