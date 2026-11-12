package app_test

import (
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// withNeeds is a task whose docker jobs need the resources.
func (w *world) withNeeds(needs string) {
	w.tasks.Put(string(call.Scope.Repository()), commit, needing(tasktest.Scripted("task", passes, scores), needs))
}

// needing is the task with docker jobs that need the resources.
func needing(tree task.Tree, needs string) task.Tree {
	manifest := task.TasksDir + "/task/" + task.ManifestName
	tree[manifest] = task.File{Data: append(tree[manifest].Data, []byte("\n[environment]\nneeds = "+needs+"\n")...)}
	return tree
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

// A docker job is refused only when no placement gives what its task needs, or when the one it is
// restricted to lacks a need, naming them, and then it queues nothing. Several placements that fit
// are no refusal: the job records its task's needs and no placement, since the scheduler chooses
// one when it starts. Its key is taken on the first of them by name. A host job runs on no
// placement, whatever it was restricted to.
func TestADockerJobMayRunOnEveryPlacementThatGivesWhatItsTaskNeeds(t *testing.T) {
	w := newWorld(t)
	w.withNeeds(`["kubernetes"]`)
	envs := &targetRecorder{Environments: fakeenv.Environments{
		core.EnvironmentDocker: fakeenv.New(core.EnvironmentDocker, w.gates), core.EnvironmentHost: w.env,
	}}
	a := newPlacementApp(w, []core.PlacementOffer{
		{Name: "a100", Resources: []core.ResourceKind{core.ResourceGPU}},
		{Name: "local", Resources: []core.ResourceKind{core.ResourceKubernetes, core.ResourceMounts}},
		{Name: "remote", Resources: []core.ResourceKind{core.ResourceKubernetes}},
	}, envs)
	docker := func(placement string) core.JobSpec {
		return core.JobSpec{Commit: commit, Environment: core.EnvironmentDocker, Placement: placement, Settings: core.CheckSettings{}}
	}
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: docker("")})
	if err != nil {
		t.Fatal(err)
	}
	if got := envs.asked(); out.Job.Spec.Placement != "" || !slices.Equal(out.Job.Needs, []core.ResourceKind{core.ResourceKubernetes}) || len(got) == 0 || got[0].Placement != "local" {
		t.Fatalf("the job is restricted to %q, needs %v, and the environment was asked for %+v; want no restriction, kubernetes, local", out.Job.Spec.Placement, out.Job.Needs, got)
	}
	restricted, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: docker("remote"), NoCache: true})
	if err != nil || restricted.Job.Spec.Placement != "remote" {
		t.Fatalf("a job restricted to remote: %+v, %v", restricted.Job.Spec, err)
	}
	_, err = a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: docker("a100")})
	if err == nil {
		t.Fatal("asking for a100 was accepted")
	}
	if f := core.AsFailure(err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, `placement "a100" has no resource "kubernetes"`) {
		t.Errorf("asking for a100: %v, want a refusal naming a100 and kubernetes", err)
	}
	host, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Placement: "a100", Settings: core.CheckSettings{}}})
	if err != nil || host.Job.Spec.Placement != "" || len(host.Job.Needs) != 0 {
		t.Errorf("a host job: %+v needing %v, %v; want no placement and no needs", host.Job.Spec, host.Job.Needs, err)
	}

	w2 := newWorld(t)
	w2.withNeeds(`["gpu"]`)
	envs2 := &targetRecorder{Environments: fakeenv.Environments{core.EnvironmentDocker: fakeenv.New(core.EnvironmentDocker, w2.gates)}}
	b := newPlacementApp(w2, []core.PlacementOffer{{Name: "local"}, {Name: "remote"}}, envs2)
	_, err = b.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: docker("")})
	if f := core.AsFailure(err); err == nil || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "local, remote") {
		t.Errorf("no placement that fits: %v, want a refusal naming the placements", err)
	}
	jobs, err := b.Jobs(ctxFor(t), scope, core.JobFilter{})
	var nf *core.Failure
	if err != nil && !(errors.As(err, &nf) && nf.Kind == core.FailureNotFound) || len(jobs) != 0 {
		t.Errorf("jobs = %v, %v; a refused submission queues nothing", jobs, err)
	}
	if got := envs2.asked(); len(got) != 0 {
		t.Errorf("the environment was asked for %+v before a placement was found", got)
	}
}

// onPlacements is a docker environment per placement, each with gates of its own, whose
// fingerprints differ only in the placement they name; and the host.
type onPlacements struct {
	host   *fakeenv.Environment
	docker map[string]*fakeenv.Environment
	gates  map[string]*fakeenv.MemoryGates
}

func newOnPlacements(w *world, names ...string) onPlacements {
	out := onPlacements{host: w.env, docker: map[string]*fakeenv.Environment{}, gates: map[string]*fakeenv.MemoryGates{}}
	for _, name := range names {
		out.gates[name] = fakeenv.NewMemoryGates()
		out.docker[name] = fakeenv.New(core.EnvironmentDocker, out.gates[name], fakeenv.WithFiles(w.files), fakeenv.WithFingerprint(core.Fingerprint{
			Kind: core.EnvironmentDocker, Backend: core.BackendCPU, ImageDigest: "sha256:" + strings.Repeat("0", 64),
			Facts: map[string]string{core.FactPlacement: name, "os": "fake"},
		}))
	}
	return out
}

func (o onPlacements) Environment(kind core.EnvironmentKind, target environment.Target) (environment.Environment, error) {
	if kind == core.EnvironmentHost {
		return o.host, nil
	}
	if e, ok := o.docker[target.Placement]; ok {
		return e, nil
	}
	return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: "no placement " + target.Placement}
}

// Placements are interchangeable. Two measures that x and y each fit start at once, one on each,
// though neither asked for one, while z, which lacks what the task needs, runs neither: each holds
// its placement alone, so a check submitted then waits, naming a measure. Once the measures end the
// check runs. Each measure records the placement it ran on.
func TestTwoMeasuresRunAtOnceEachAloneOnItsOwnPlacement(t *testing.T) {
	w := newWorld(t)
	w.tasks.Put(string(call.Scope.Repository()), commit, needing(tasktest.Scripted("task", passes,
		[]string{"mark", "benching", ";", "hold", "bench", ";", "emit", `{"ops": 7.5}`}), `["kubernetes"]`))
	kube := []core.ResourceKind{core.ResourceKubernetes}
	w.scheduler = app.Scheduler{
		Resources: []schedule.Resource{
			{ID: schedule.PlacementOf("x"), Capacity: 2}, {ID: schedule.PlacementOf("y"), Capacity: 2}, {ID: schedule.PlacementOf("z"), Capacity: 2}, {ID: "host", Capacity: 1},
		},
		Demand: schedule.PlacementDemand{Placements: map[string]schedule.Offered{"x": {Resources: kube}, "y": {Resources: kube}, "z": {}}, Host: "host"},
	}
	envs := newOnPlacements(w, "x", "y", "z")
	a := newPlacementApp(w, []core.PlacementOffer{{Name: "x", Resources: kube}, {Name: "y", Resources: kube}, {Name: "z"}}, &targetRecorder{Environments: envs})
	ctx := ctxFor(t)
	submit := func(settings core.Settings) core.Job {
		t.Helper()
		spec := core.JobSpec{Commit: commit, Environment: core.EnvironmentDocker, Settings: settings}
		out, err := a.Submit(ctx, app.SubmitRequest{Context: call, Spec: spec, NoCache: true, NoPlan: noPlan(spec)})
		if err != nil {
			t.Fatal(err)
		}
		return out.Job
	}
	first, second := submit(core.MeasureSettings{Samples: 1}), submit(core.MeasureSettings{Samples: 1})
	for _, p := range []string{"x", "y"} {
		if !envs.gates[p].Reached(ctx, "benching") {
			t.Fatalf("no measure started on %s", p)
		}
	}
	if n := envs.docker["x"].Active() + envs.docker["y"].Active(); n != 2 {
		t.Fatalf("%d jobs run on x and y, want the two measures at once", n)
	}
	check := submit(core.CheckSettings{})
	wait := a.Waits(scope, []core.JobID{check.ID})[check.ID]
	if wait.Kind != core.WaitHeldExclusively || len(wait.Holders) != 1 || (wait.Holders[0].Job != first.ID && wait.Holders[0].Job != second.ID) {
		t.Fatalf("the check waits as %+v; want a placement a measure holds alone", wait)
	}
	ran := map[string]core.JobID{}
	for _, m := range []core.Job{first, second} {
		got, err := a.Job(ctx, scope, m.ID)
		if err != nil || got.Environment == nil {
			t.Fatalf("measure %s: %+v, %v", m.ID, got, err)
		}
		ran[got.Environment.Placement()] = m.ID
	}
	if len(ran) != 2 || ran["x"] == "" || ran["y"] == "" {
		t.Fatalf("the measures ran on %v, want one on x and one on y", ran)
	}
	for _, p := range []string{"x", "y"} {
		envs.gates[p].Release("bench")
	}
	for _, m := range []core.Job{first, second} {
		awaitState(t, a, m.ID, core.JobSucceeded)
	}
	awaitState(t, a, check.ID, core.JobSucceeded)
	if envs.docker["z"].Fingerprinted() != 0 || envs.gates["z"].Marked("benching") {
		t.Fatal("a job ran on z, which lacks what the task needs")
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
		Demand: schedule.PlacementDemand{Placements: map[string]schedule.Offered{
			"a": {Resources: []core.ResourceKind{core.ResourceKubernetes}, Claims: []schedule.Claim{cluster}},
			"b": {Resources: []core.ResourceKind{core.ResourceKubernetes}, Claims: []schedule.Claim{cluster}},
			"c": {},
		}, Host: "host"},
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
			Image: core.ImageHarness, Argv: []string{"mark", "started-" + name, ";", "hold", name, ";", "exit", "0"},
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

	out, err := a.Submit(ctx, app.SubmitRequest{Context: call, NoPlan: true, Spec: core.JobSpec{
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

// withDockerfile gives the task a Dockerfile, so its check and measure run in docker
// (core.ChooseEnvironment).
func (w *world) withDockerfile(check, bench []string) {
	tree := tasktest.Scripted("task", check, bench)
	tree[task.TasksDir+"/task/Dockerfile"] = task.File{Data: []byte("FROM scratch\n")}
	w.tasks.Put(string(call.Scope.Repository()), commit, tree)
}

// A command job that leaves its environment to the task runs where the task's check and measure
// run, so on a task with a Dockerfile it is a docker job on its placement, and it waits behind a
// measure that holds that placement alone like any other job there. Before, it ran on the host,
// without the task's image, and claimed only the host's slot, so it started beside the measure
// and its load fell into the measure's samples.
func TestACommandOfNoEnvironmentRunsWhereItsTaskRunsAndWaitsBehindAMeasure(t *testing.T) {
	w := newWorld(t)
	w.withDockerfile(passes, []string{"mark", "benching", ";", "hold", "bench", ";", "emit", `{"ops": 7.5}`})
	w.scheduler = app.Scheduler{
		Resources: []schedule.Resource{{ID: schedule.PlacementOf("local"), Capacity: 2}, {ID: "host", Capacity: 2}},
		Demand:    schedule.PlacementDemand{Placements: map[string]schedule.Offered{"local": {}}, Host: "host"},
	}
	docker := fakeenv.New(core.EnvironmentDocker, w.gates, fakeenv.WithFiles(w.files))
	envs := &targetRecorder{Environments: fakeenv.Environments{core.EnvironmentDocker: docker, core.EnvironmentHost: w.env}}
	a := newPlacementApp(w, []core.PlacementOffer{{Name: "local"}}, envs)
	ctx := ctxFor(t)

	measured, err := a.Submit(ctx, app.SubmitRequest{Context: call, NoPlan: true, Spec: core.JobSpec{Commit: commit, Placement: "local", Settings: core.MeasureSettings{Samples: 1}}})
	if err != nil {
		t.Fatal(err)
	}
	if !w.gates.Reached(ctx, "benching") {
		t.Fatal("the measure did not start")
	}
	out, err := a.Submit(ctx, app.SubmitRequest{Context: call, Spec: core.JobSpec{Commit: commit, Placement: "local", Settings: core.CommandSettings{
		Image: core.ImageHarness, Argv: []string{"mark", "started", ";", "exit", "0"},
	}}})
	if err != nil {
		t.Fatal(err)
	}
	cmd := out.Job
	if cmd.Spec.Environment != core.EnvironmentDocker || cmd.Spec.Placement != "local" {
		t.Fatalf("the command was queued in %q on %q; want docker on local, where the task's measure runs", cmd.Spec.Environment, cmd.Spec.Placement)
	}
	if wait := a.Waits(scope, []core.JobID{cmd.ID})[cmd.ID]; wait.Kind != core.WaitHeldExclusively || len(wait.Holders) != 1 || wait.Holders[0].Job != measured.Job.ID {
		t.Fatalf("the command waits as %+v; want behind the measure that holds local alone", wait)
	}
	w.gates.Release("bench")
	awaitState(t, a, measured.Job.ID, core.JobSucceeded)
	if got := awaitFinal(t, a, cmd.ID); got.State != core.JobSucceeded {
		t.Fatalf("the command ended %s", got.State)
	}
	if n := w.ran("started"); n != 0 {
		t.Errorf("the command ran %d times on the host", n)
	}
}

// For every task setup and every environment a submission can ask for, a command job is queued in
// the environment and on the placement a check of the same submission is: a command never runs
// somewhere the task's own jobs do not. A host job records no placement, whatever was asked.
func TestACommandJobIsPlacedAsACheckOfTheSameSubmissionIs(t *testing.T) {
	for _, dockerfile := range []bool{false, true} {
		for _, requested := range []core.EnvironmentKind{core.EnvironmentAuto, core.EnvironmentHost, core.EnvironmentDocker} {
			for _, placement := range []string{"", "local"} {
				t.Run(fmt.Sprintf("dockerfile=%t/env=%q/placement=%q", dockerfile, requested, placement), func(t *testing.T) {
					w := newWorld(t)
					if dockerfile {
						w.withDockerfile(passes, scores)
					} else {
						w.withTask(passes, scores)
					}
					envs := &targetRecorder{Environments: fakeenv.Environments{
						core.EnvironmentDocker: fakeenv.New(core.EnvironmentDocker, w.gates), core.EnvironmentHost: w.env,
					}}
					a := newPlacementApp(w, []core.PlacementOffer{{Name: "local"}}, envs)
					spec := func(settings core.Settings) core.JobSpec {
						return core.JobSpec{Commit: commit, Environment: requested, Placement: placement, Settings: settings}
					}
					check, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec(core.CheckSettings{})})
					if err != nil {
						t.Fatal(err)
					}
					cmd, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec(core.CommandSettings{Image: core.ImageHarness, Argv: []string{"exit", "0"}})})
					if err != nil {
						t.Fatal(err)
					}
					want, got := check.Job.Spec, cmd.Job.Spec
					if got.Environment != want.Environment || got.Placement != want.Placement {
						t.Errorf("the command is queued in %q on %q, the check in %q on %q", got.Environment, got.Placement, want.Environment, want.Placement)
					}
					if got.Environment == core.EnvironmentHost && got.Placement != "" {
						t.Errorf("a host command records the placement %q", got.Placement)
					}
				})
			}
		}
	}
}
