package app_test

import (
	"fmt"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// poolRig is a daemon whose machine has a pool of eight GPUs that host jobs draw from, as a local
// placement with a count does.
func poolRig(t *testing.T) (*world, *sync.Mutex, *[]lifecycle.Launch) {
	t.Helper()
	w := newWorld(t)
	var err error
	if w.hostPool, err = core.ParseGPUCount(8, "test"); err != nil {
		t.Fatal(err)
	}
	w.gpuTask()
	pool := &schedule.Pool{Resource: "gpu:local", Size: 8, Kind: core.ResourceGPU}
	w.scheduler = app.Scheduler{
		Resources: []schedule.Resource{{ID: schedule.PlacementOf("local"), Capacity: 8}, {ID: "gpu:local", Capacity: 8, Devices: true}},
		Demand: schedule.PlacementDemand{
			Placements: map[string]schedule.Offered{"local": {Pool: pool}},
			Host:       schedule.PlacementOf("local"), HostPool: pool,
		},
	}
	var mu sync.Mutex
	var seen []lifecycle.Launch
	w.wrap = func(s supervisor.Supervisors) supervisor.Supervisors {
		return launches{Supervisors: s, mu: &mu, seen: &seen}
	}
	return w, &mu, &seen
}

func gpuJob(t *testing.T, a *app.App, n int, script ...string) core.Job {
	t.Helper()
	spec := command(script...)
	var err error
	if spec.GPUs, err = core.ParseGPUCount(uint32(n), "test"); err != nil {
		t.Fatal(err)
	}
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	return out.Job
}

func heldBy(j core.Job) []core.DeviceIndex {
	if j.GPUs == nil {
		return nil
	}
	return j.GPUs.Devices.Devices()
}

// Jobs that ask for GPUs run at once on disjoint devices while the pool has them, and one that
// needs more than are free waits, saying how many. The grant is in the job's record by the time
// its command runs, and the supervisor's launch carries the same one. A daemon that restarts while
// they run keeps their devices held: a job it starts then is given none of them.
func TestJobsGetDisjointGPUsOfThePoolAndAFreshDaemonKeepsThemHeld(t *testing.T) {
	w, mu, seen := poolRig(t)
	a := w.newApp()
	ctx := ctxFor(t)
	held := func(n int) []string {
		return []string{"mark", fmt.Sprintf("started-%d", n), ";", "hold", fmt.Sprintf("g%d", n), ";", "exit", "0"}
	}
	first := gpuJob(t, a, 3, held(0)...)
	second := gpuJob(t, a, 3, held(1)...)
	for n := range 2 {
		if !w.gates.Reached(ctx, fmt.Sprintf("started-%d", n)) {
			t.Fatalf("job %d did not start beside the other", n)
		}
	}
	big := gpuJob(t, a, 4, held(2)...)
	if got := a.Waits(scope, []core.JobID{big.ID})[big.ID]; got.Kind != core.WaitFull || got.Resource != "gpu:local" {
		t.Fatalf("the job that asks for 4 GPUs of the 2 free waits as %+v, want a wait on the full pool", got)
	} else if got.GPUsWanted != 4 || got.GPUsFree != 2 {
		t.Fatalf("the wait says it wants %d GPUs with %d free, want 4 and 2", got.GPUsWanted, got.GPUsFree)
	} else if len(got.Holders) != 2 || got.Holders[0].GPUs == nil || len(heldBy(core.Job{GPUs: got.Holders[0].GPUs})) != 3 {
		t.Fatalf("the wait names its holders %+v, want the two jobs of 3 devices each", got.Holders)
	}
	// The placement's pool view says 2 of 8 are free, which jobs hold the others and which waits.
	view, ok := a.PoolOf("local")
	if !ok || view.Size != 8 || view.Free != 2 || len(view.Holders) != 2 || len(view.Waiting) != 1 ||
		view.Waiting[0].Job != big.ID || view.Waiting[0].Wanted != 4 {
		t.Fatalf("the pool view is %+v (%v), want 2 of 8 free, two holders and the job of 4 waiting", view, ok)
	}
	for _, h := range view.Holders {
		if h.Grant.Devices.Len() != 3 || h.Kind != core.JobCommand {
			t.Fatalf("holder %+v, want a command of 3 devices", h)
		}
	}
	if _, ok := a.PoolOf("elsewhere"); ok {
		t.Fatal("a placement the config does not have has a pool")
	}
	// The record has each job's devices, which are what its launch carried, and disjoint.
	grants := map[core.JobID][]core.DeviceIndex{}
	for _, id := range []core.JobID{first.ID, second.ID} {
		j := awaitState(t, a, id, core.JobRunning)
		if len(heldBy(j)) != 3 || j.GPUs.Source != core.GrantByCLI || j.GPUs.Pool != "gpu:local" || j.GPUs.Whole {
			t.Fatalf("job %s holds %+v, want 3 devices of gpu:local chosen by the CLI", id, j.GPUs)
		}
		grants[id] = heldBy(j)
	}
	mu.Lock()
	for _, l := range *seen {
		if l.GPUs == nil || !slices.Equal(l.GPUs.Devices.Devices(), grants[l.Job]) {
			t.Errorf("the launch of %s carried %+v, want the recorded devices %v", l.Job, l.GPUs, grants[l.Job])
		}
	}
	mu.Unlock()
	for _, d := range grants[first.ID] {
		if slices.Contains(grants[second.ID], d) {
			t.Fatalf("the two jobs hold device %d at once: %v and %v", d, grants[first.ID], grants[second.ID])
		}
	}

	// The daemon restarts while both run. The job that waits for 4 keeps its turn, and a later one
	// of 2 does not take the 2 free devices ahead of it. When the first job ends, the big one is
	// given devices that the job still running does not hold, as recovered from its record.
	a.Close()
	b := w.newApp()
	small := gpuJob(t, b, 2, held(3)...)
	w.gates.Release("g0")
	if !w.gates.Reached(ctx, "started-2") {
		t.Fatal("the job that asks for 4 did not start once the first ended")
	}
	jobs, err := b.Jobs(ctx, scope, core.JobFilter{})
	if err != nil {
		t.Fatal(err)
	}
	for _, j := range jobs {
		if j.ID == small.ID && (j.State != core.JobQueued || j.GPUs != nil) {
			t.Fatalf("the job of 2 is %s and holds %v beside the job of 4 that waited ahead of it", j.State, heldBy(j))
		}
	}
	bigDevices := heldBy(awaitState(t, b, big.ID, core.JobRunning))
	if len(bigDevices) != 4 {
		t.Fatalf("the job holds %v, want 4 devices", bigDevices)
	}
	for _, d := range bigDevices {
		if slices.Contains(grants[second.ID], d) {
			t.Fatalf("after the restart the job was given device %d, which the job that kept running holds (%v)", d, grants[second.ID])
		}
	}
	w.gates.Release("g1")
	if !w.gates.Reached(ctx, "started-3") {
		t.Fatal("the job that asks for 2 did not start once 4 were free")
	}
	for _, d := range heldBy(awaitState(t, b, small.ID, core.JobRunning)) {
		if slices.Contains(bigDevices, d) {
			t.Fatalf("the last job was given device %d, which the big one holds (%v)", d, bigDevices)
		}
	}
	w.gates.Release("g2")
	w.gates.Release("g3")
	for _, id := range []core.JobID{first.ID, second.ID, big.ID, small.ID} {
		awaitState(t, b, id, core.JobSucceeded)
	}
}

// A count the pool cannot give, or one on a measure, is refused when the job is submitted, and
// nothing is queued: the refusal names --gpus, and the sizes the pool allows.
func TestSubmitRefusesAGPUCountThePoolCannotGive(t *testing.T) {
	w, _, _ := poolRig(t)
	a := w.newApp()
	ask := func(spec core.JobSpec, n int) error {
		var err error
		if spec.GPUs, err = core.ParseGPUCount(uint32(n), "test"); err != nil {
			t.Fatal(err)
		}
		_, err = a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec, NoPlan: noPlan(spec)})
		return err
	}
	if err := ask(command("exit", "0"), 9); err == nil || !strings.Contains(err.Error(), "--gpus 9") || !strings.Contains(err.Error(), "1 to 8") {
		t.Errorf("9 GPUs of a pool of 8: %v, want a refusal that names the flag and the sizes", err)
	}
	if err := ask(flowSpec(core.MeasureSettings{}), 2); err == nil || !strings.Contains(err.Error(), "--gpus: a measure takes the whole node") {
		t.Errorf("a measure of 2 GPUs: %v, want a refusal that names the flag", err)
	}
	jobs, err := a.Jobs(ctxFor(t), scope, core.JobFilter{})
	if err != nil || len(jobs) != 0 {
		t.Errorf("after the refusals: %d jobs, %v; want none queued", len(jobs), err)
	}
}

// gpuTask puts a task into the repository that needs a GPU, as a task of the cuda backend with
// `needs = ["gpu"]` does.
func (w *world) gpuTask() {
	tree := tasktest.Scripted("task", passes, scores)
	manifest := taskDir + task.ManifestName
	data := strings.Replace(string(tree[manifest].Data), `backends = ["cpu"]`, "backends = [\"cuda\"]\n\n[environment]\nneeds = [\"gpu\"]\n", 1)
	tree[manifest] = task.File{Data: []byte(data)}
	w.tasks.Put(string(call.Scope.Repository()), commit, tree)
}

// On a machine with a pool, a job on the host is asked for --gpus as a docker job is, whatever its
// kind: a host job given none of the pool would see every GPU while the scheduler counted them
// free. A measure takes the whole pool and refuses a count. Without a pool a host job asks for none
// and is refused one, as it always was.
func TestHostJobsOfATaskThatNeedsGPUsAreAskedForTheirCountWhereThereIsAPool(t *testing.T) {
	host := func(spec core.JobSpec, n int) core.JobSpec {
		spec.Environment = core.EnvironmentHost
		if n > 0 {
			var err error
			if spec.GPUs, err = core.ParseGPUCount(uint32(n), "test"); err != nil {
				t.Fatal(err)
			}
		}
		return spec
	}
	harness := func() core.JobSpec {
		spec := command("exit", "0")
		c := spec.Settings.(core.CommandSettings)
		c.Image = core.ImageHarness
		spec.Settings = c
		return spec
	}
	submit := func(a *app.App, spec core.JobSpec) (core.Job, error) {
		out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec, NoPlan: noPlan(spec)})
		return out.Job, err
	}
	kinds := map[string]func() core.JobSpec{
		"check":   func() core.JobSpec { return flowSpec(core.CheckSettings{}) },
		"command": harness,
	}

	t.Run("with a pool", func(t *testing.T) {
		w, _, _ := poolRig(t)
		a := w.newApp()
		for name, spec := range kinds {
			if _, err := submit(a, host(spec(), 0)); err == nil || !strings.Contains(err.Error(), "--gpus N") {
				t.Errorf("a host %s with no count: %v, want a refusal that asks for --gpus", name, err)
			}
			if _, err := submit(a, host(spec(), 9)); err == nil || !strings.Contains(err.Error(), "1 to 8") {
				t.Errorf("a host %s of 9 GPUs: %v, want a refusal that names the sizes", name, err)
			}
			if _, err := submit(a, host(spec(), 2)); err != nil {
				t.Errorf("a host %s of 2 GPUs: %v", name, err)
			}
		}
		if _, err := submit(a, host(flowSpec(core.MeasureSettings{}), 2)); err == nil || !strings.Contains(err.Error(), "--gpus: a measure takes the whole node") {
			t.Errorf("a host measure of 2 GPUs: %v, want a refusal", err)
		}
		measure, err := submit(a, host(flowSpec(core.MeasureSettings{}), 0))
		if err != nil {
			t.Fatalf("a host measure: %v", err)
		}
		got := awaitFinal(t, a, measure.ID)
		if got.GPUs == nil || !got.GPUs.Whole || got.GPUs.Devices.Len() != 8 {
			t.Errorf("the host measure was given %+v, want the whole pool of 8", got.GPUs)
		}
	})

	t.Run("without a pool", func(t *testing.T) {
		w := newWorld(t)
		w.gpuTask()
		a := w.newApp()
		for name, spec := range kinds {
			if _, err := submit(a, host(spec(), 0)); err != nil {
				t.Errorf("a host %s with no count and no pool: %v", name, err)
			}
			if _, err := submit(a, host(spec(), 2)); err == nil || !strings.Contains(err.Error(), "--gpus") {
				t.Errorf("a host %s of 2 GPUs with no pool: %v, want a refusal that names --gpus", name, err)
			}
		}
		if _, err := submit(a, host(flowSpec(core.MeasureSettings{}), 0)); err != nil {
			t.Errorf("a host measure with no pool: %v", err)
		}
	})
}
