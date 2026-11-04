package app_test

import (
	"errors"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// withNeeds is a task whose docker jobs need the resources.
func (w *world) withNeeds(needs string) {
	tree := tasktest.Scripted("task", passes, scores)
	manifest := task.TasksDir + "/task/" + task.ManifestName
	tree[manifest] = task.File{Data: append(tree[manifest].Data, []byte("\n[environment]\nneeds = "+needs+"\n")...)}
	w.tasks.Put(string(call.Scope.Repository()), commit, tree)
}

func newPlacementApp(w *world, offers []core.PlacementOffer, envs *targetRecorder) *app.App {
	a := app.New(instant(app.Config{
		Clock: w.clock, Stores: w.stores, Environments: envs, Supervisors: w.supervisors(),
		Tasks: w.taskSource(), Repositories: w.git, Placements: offers,
	}))
	w.apps = append(w.apps, a)
	if err := a.Start(ctxFor(w.t)); err != nil {
		w.t.Fatal(err)
	}
	return a
}

// A docker job with no placement asked for runs on the one placement that gives what its task
// needs: its spec records it and the environment is asked for it; one that asks for a placement
// lacking a need, or for none when several fit, is refused, naming them, and queues nothing. A
// host job has no placement, whatever was asked.
func TestADockerJobRunsOnThePlacementThatGivesWhatItsTaskNeeds(t *testing.T) {
	w := newWorld(t)
	w.withNeeds(`["kubernetes"]`)
	envs := &targetRecorder{Environments: fakeenv.Environments{
		core.EnvironmentDocker: fakeenv.New(core.EnvironmentDocker, w.gates), core.EnvironmentHost: w.env,
	}}
	a := newPlacementApp(w, []core.PlacementOffer{
		{Name: "a100", Resources: []core.ResourceKind{core.ResourceGPU}},
		{Name: "local", Resources: []core.ResourceKind{core.ResourceKubernetes, core.ResourceMounts}},
	}, envs)
	docker := func(placement string) core.JobSpec {
		return core.JobSpec{Commit: commit, Environment: core.EnvironmentDocker, Placement: placement, Settings: core.CheckSettings{}}
	}
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: docker("")})
	if err != nil {
		t.Fatal(err)
	}
	if got := envs.asked(); out.Job.Spec.Placement != "local" || len(got) == 0 || got[0].Placement != "local" {
		t.Fatalf("the job's placement is %q and the environment was asked for %+v; want local", out.Job.Spec.Placement, got)
	}
	_, err = a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: docker("a100")})
	if err == nil {
		t.Fatal("asking for a100 was accepted")
	}
	if f := core.AsFailure(err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, `placement "a100" has no resource "kubernetes"`) {
		t.Errorf("asking for a100: %v, want a refusal naming a100 and kubernetes", err)
	}
	host, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Placement: "a100", Settings: core.CheckSettings{}}})
	if err != nil || host.Job.Spec.Placement != "" {
		t.Errorf("a host job: %+v, %v; want no placement", host.Job.Spec, err)
	}

	w2 := newWorld(t)
	w2.withNeeds(`[]`)
	envs2 := &targetRecorder{Environments: fakeenv.Environments{core.EnvironmentDocker: fakeenv.New(core.EnvironmentDocker, w2.gates)}}
	b := newPlacementApp(w2, []core.PlacementOffer{{Name: "a100"}, {Name: "local"}}, envs2)
	_, err = b.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: docker("")})
	if err == nil {
		t.Fatal("a job that two placements fit was accepted")
	}
	if f := core.AsFailure(err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "a100, local") {
		t.Errorf("two placements that fit: %v, want a refusal naming both", err)
	}
	jobs, err := b.Jobs(ctxFor(t), scope, core.JobFilter{})
	var nf *core.Failure
	if err != nil && !(errors.As(err, &nf) && nf.Kind == core.FailureNotFound) || len(jobs) != 0 {
		t.Errorf("jobs = %v, %v; a refused submission queues nothing", jobs, err)
	}
	if got := envs2.asked(); len(got) != 0 {
		t.Errorf("the environment was asked for %+v before a placement was chosen", got)
	}
}
