package app_test

import (
	"fmt"
	"slices"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// poolRig is a daemon whose machine has a pool of eight GPUs that host jobs draw from, as a local
// placement with a count does.
func poolRig(t *testing.T) (*world, *sync.Mutex, *[]lifecycle.Launch) {
	t.Helper()
	w := newWorld(t)
	pool := &schedule.Pool{Resource: "gpu:local", Size: 8, Kind: core.ResourceGPU}
	w.scheduler = app.Scheduler{
		Resources: []schedule.Resource{{ID: schedule.PlacementOf("local"), Capacity: 8}, {ID: "gpu:local", Capacity: 8, Devices: true}},
		Demand:    schedule.PlacementDemand{Host: schedule.PlacementOf("local"), HostPool: pool},
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
