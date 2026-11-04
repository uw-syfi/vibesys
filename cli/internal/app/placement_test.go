package app_test

import (
	"errors"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
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
	supervisors := supervisor.NewInProcess(envs.Environments, w.checkouts, w.survivors).WithHost(supervisor.Host{Files: w.files, Clock: w.clock})
	a := app.New(instant(app.Config{
		Clock: w.clock, Stores: w.stores, Environments: envs, Supervisors: supervisors,
		Tasks: w.taskSource(), Repositories: w.git, Placements: offers, Scheduler: w.scheduler,
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

// A daemon of three placements, a and b on one cluster and c on none, runs the jobs of two
// placements at once. A measure on a holds a and the cluster for itself alone: a command on c runs
// beside it, while one on b waits for the cluster and one on a for the placement, each saying the
// measure holds it. Both start once the measure ends.
func TestAMeasureHoldsItsPlacementAndClusterAloneWhileOtherPlacementsRun(t *testing.T) {
	w := newWorld(t)
	w.tasks.Put(string(call.Scope.Repository()), commit, tasktest.Scripted("task", passes,
		[]string{"mark", "benching", ";", "hold", "bench", ";", "emit", `{"ops": 7.5}`}))
	cluster := schedule.Claim{Resource: "kubernetes:m/k", Amount: 1}
	w.scheduler = app.Scheduler{
		Resources: []schedule.Resource{
			{ID: schedule.PlacementOf("a"), Capacity: 2}, {ID: schedule.PlacementOf("b"), Capacity: 2},
			{ID: schedule.PlacementOf("c"), Capacity: 2}, {ID: cluster.Resource, Capacity: 4}, {ID: "host", Capacity: 1},
		},
		Demand: schedule.PlacementDemand{Placements: map[string][]schedule.Claim{"a": {cluster}, "b": {cluster}}, Host: "host"},
	}
	docker := fakeenv.New(core.EnvironmentDocker, w.gates, fakeenv.WithFiles(w.files))
	envs := &targetRecorder{Environments: fakeenv.Environments{core.EnvironmentDocker: docker, core.EnvironmentHost: w.env}}
	a := newPlacementApp(w, []core.PlacementOffer{
		{Name: "a", Resources: []core.ResourceKind{core.ResourceKubernetes}},
		{Name: "b", Resources: []core.ResourceKind{core.ResourceKubernetes}},
		{Name: "c"},
	}, envs)
	ctx := ctxFor(t)
	on := func(placement, name string) core.Job {
		t.Helper()
		spec := core.JobSpec{Commit: commit, Environment: core.EnvironmentDocker, Placement: placement, Settings: core.CommandSettings{
			Argv: []string{"mark", "started-" + name, ";", "hold", name, ";", "exit", "0"},
		}}
		out, err := a.Submit(ctx, app.SubmitRequest{Context: call, Spec: spec})
		if err != nil {
			t.Fatal(err)
		}
		if out.Job.Spec.Placement != placement {
			t.Fatalf("the command asked for %q runs on %q", placement, out.Job.Spec.Placement)
		}
		return out.Job
	}

	first := []core.Job{on("a", "a1"), on("b", "b1")}
	for _, name := range []string{"a1", "b1"} {
		if !w.gates.Reached(ctx, "started-"+name) {
			t.Fatalf("%s did not start beside the job of the other placement", name)
		}
	}
	if n := docker.Active(); n != 2 {
		t.Fatalf("%d jobs run, want the two placements' jobs at once", n)
	}
	for i, name := range []string{"a1", "b1"} {
		w.gates.Release(name)
		awaitState(t, a, first[i].ID, core.JobSucceeded)
	}

	out, err := a.Submit(ctx, app.SubmitRequest{Context: call, Spec: core.JobSpec{
		Commit: commit, Environment: core.EnvironmentDocker, Placement: "a", Settings: core.MeasureSettings{Samples: 1},
	}})
	if err != nil {
		t.Fatal(err)
	}
	measure := out.Job
	if !w.gates.Reached(ctx, "benching") {
		t.Fatal("the measure did not start")
	}
	beside := on("c", "c1")
	if !w.gates.Reached(ctx, "started-c1") {
		t.Fatal("the command on c did not start beside the measure on a")
	}
	onB, onA := on("b", "b2"), on("a", "a2")
	waits := a.Waits(scope, []core.JobID{onB.ID, onA.ID})
	if w := waits[onB.ID]; w.Kind != core.WaitHeldExclusively || w.Resource != string(cluster.Resource) || len(w.Holders) != 1 || w.Holders[0].Job != measure.ID {
		t.Fatalf("the command on b waits as %+v; want the cluster, held by the measure alone", w)
	}
	if w := waits[onA.ID]; w.Kind != core.WaitHeldExclusively || len(w.Holders) != 1 || w.Holders[0].Job != measure.ID {
		t.Fatalf("the command on a waits as %+v; want what the measure holds alone", w)
	}
	if n := docker.Active(); n != 2 {
		t.Fatalf("%d jobs run beside each other, want the measure and the command on c", n)
	}
	w.gates.Release("bench")
	awaitState(t, a, measure.ID, core.JobSucceeded)
	for _, name := range []string{"b2", "a2"} {
		if !w.gates.Reached(ctx, "started-"+name) {
			t.Fatalf("%s did not start once the measure ended", name)
		}
	}
	for _, j := range []struct {
		job  core.Job
		gate string
	}{{beside, "c1"}, {onB, "b2"}, {onA, "a2"}} {
		w.gates.Release(j.gate)
		awaitState(t, a, j.job.ID, core.JobSucceeded)
	}
}
