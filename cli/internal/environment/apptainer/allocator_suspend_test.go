package apptainer_test

import (
	"context"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

func isEnded(o alloc.Observation) bool {
	_, ok := o.(alloc.SeenEnded)
	return ok
}

// Regression: a standing allocation whose Slurm job is suspended lives. It is never observed as
// ended (the core would then mark it released without a cancel, and the job would resume untracked),
// a daemon that restarted adopts it, a Request of its ID finds it instead of submitting a second job,
// and it is observed as running with no time added: no start and no end said. It runs again, with the
// time limit it had, once resumed.
func TestASuspendedStandingAllocationLivesAndIsAdopted(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	a := newAllocator(t, cluster.Connect())
	h, err := a.Request(context.Background(), spec(1, "run-a", 2*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	cluster.Advance(10 * time.Minute)
	if !cluster.Suspend(h.Ref) {
		t.Fatal("the allocation does not run")
	}
	for step := range 3 {
		o, err := a.Observe(context.Background(), h)
		run, ok := o.(alloc.SeenRunning)
		if err != nil || !ok || !run.StartedAt.IsZero() || !run.EndsAt.IsZero() || run.Node != clusterfake.NodeName {
			t.Fatalf("step %d: a suspended allocation is observed %+v, %v; want running, on its node, with no times", step, o, err)
		}
		cluster.Advance(time.Hour) // the limit does not count the suspension
	}
	if got, err := newAllocator(t, cluster.Connect()).Adopt(context.Background()); err != nil || !slices.Equal(got, []alloc.Handle{h}) {
		t.Fatalf("Adopt = %+v, %v; want the suspended allocation %+v", got, err, h)
	}
	if again, err := a.Request(context.Background(), spec(1, "run-a", 2*time.Hour)); err != nil || again != h {
		t.Fatalf("Request = %+v, %v; want the same allocation %+v", again, err, h)
	}
	if n := len(cluster.Submitted()); n != 1 {
		t.Fatalf("%d jobs submitted, want 1", n)
	}
	cluster.Resume(h.Ref)
	o, _ := a.Observe(context.Background(), h)
	run, ok := o.(alloc.SeenRunning)
	if !ok || run.EndsAt.Sub(run.StartedAt) != 2*time.Hour || !run.EndsAt.After(clusterfake.Epoch.Add(110*time.Minute)) {
		t.Fatalf("after the resume: %+v; want the 2h limit, shifted by the suspension", o)
	}
	cluster.Advance(2 * time.Hour)
	if o, _ := a.Observe(context.Background(), h); !isEnded(o) {
		t.Errorf("after its limit: %+v, want an end", o)
	}
}

// A job that is suspended starts no container: the engine refuses it, as for a job that does not
// run, and leaves the allocation as it is.
func TestAContainerIsRefusedASuspendedStandingAllocation(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	h, err := newAllocator(t, cluster.Connect()).Request(context.Background(), spec(1, "run-a", time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	cluster.Suspend(h.Ref)
	r := newRig(t, w, cluster, setup{})
	_, err = r.engine.Start(context.Background(), containerIn(t, r, "vibesys-cli-x", h.Ref))
	if f := core.AsFailure(err); err == nil || f.Kind != core.FailureUnavailable {
		t.Fatalf("Start = %v, want unavailable", err)
	}
	if live := cluster.Live(); len(live) != 1 {
		t.Errorf("live %v, want the allocation alone", live)
	}
}
