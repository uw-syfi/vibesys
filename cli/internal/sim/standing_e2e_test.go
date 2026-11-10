package sim_test

import (
	"context"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/production"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
)

// The operator config of a user who turned allocations on for an apptainer placement is the
// daemon's whole wiring of them: a standing allocation is requested, a command job submitted into
// any runs in it as a job joining that batch job (the cluster is asked for nothing else), a renewal
// order makes exactly one successor, and ending the allocation once its jobs are done leaves
// nothing of it on the cluster.
func TestAStandingAllocationOfAnApptainerPlacementRunsAJobInIt(t *testing.T) {
	r := newStandingRig(t, standingConfig)
	a, srv := r.start()
	ctx := r.ctx()

	first, err := a.RequestAllocation(ctx, standingCall.Run, clusterPlacement, time.Hour)
	if err != nil {
		t.Fatalf("RequestAllocation: %v", err)
	}
	if !first.Slurm {
		t.Errorf("the allocation %s of an apptainer placement is not Slurm's", first.ID)
	}
	running := r.await(a, first.ID, app.AllocUntil{Kind: app.UntilRunning})
	standing := running.Handle.Ref
	if standing == "" {
		t.Fatal("the running allocation has no batch job")
	}
	submitted := r.cluster.Submitted()
	if len(submitted) != 1 || !strings.HasPrefix(submitted[0].Name, "vibesys-apptainer-standing-") {
		t.Fatalf("submitted %+v, want the standing allocation alone", submitted)
	}

	job := r.jobIn(a, core.AllocationAny, "mark", "in-standing", ";", "hold", "g1", ";", "exit", "0")
	if !r.gates.Reached(ctx, "in-standing") {
		t.Fatal("the job did not start")
	}
	if boxes := r.cluster.StartedIn(standing); len(boxes) == 0 {
		t.Fatalf("no process of the job ran in the standing allocation %s", standing)
	}
	if n := len(r.cluster.Submitted()); n != 1 {
		t.Fatalf("%d batch jobs submitted after the job started, want the standing one alone", n)
	}
	r.gates.Release("g1")
	awaitJob(t, a, job.ID, core.JobSucceeded)
	if n := len(r.cluster.Submitted()); n != 1 {
		t.Fatalf("%d batch jobs submitted after the job ended, want the standing one alone", n)
	}
	if live := r.cluster.Live(); len(live) != 1 {
		t.Fatalf("live after the job ended: %v, want the standing allocation", live)
	}

	// The capacity of the placement says what the cluster says and the run's allocation.
	r.cluster.SetPartition(apptainerFacts(8, 3))
	resp, err := srv.ReadPlacementCapacity(ctx, &cliv1.ReadPlacementCapacityRequest{Placement: clusterPlacement, RunId: string(standingCall.Run)})
	if err != nil {
		t.Fatalf("ReadPlacementCapacity: %v", err)
	}
	if c := resp.GetCapacity(); c.GetSlotsTotal() != 8 || c.GetSlotsFree() != 3 || len(c.GetHeld()) != 1 || c.GetSlurm().GetNodesTotal() != 8 {
		t.Fatalf("capacity = %v, want 3 of 8 free, one held, Slurm's detail", c)
	}

	// A renewal order, with the lead before the end, makes exactly one successor.
	order := alloc.RenewalOrder{Lead: 10 * time.Minute, For: time.Hour, Until: alloc.UntilRunEnd{}}
	if _, err := a.RenewAllocation(ctx, standingCall.Run, first.ID, &order); err != nil {
		t.Fatalf("RenewAllocation: %v", err)
	}
	r.passes(55 * time.Minute)
	var next alloc.ID
	for next == "" {
		r.tick()
		v, err := a.Allocation(first.ID)
		if err != nil {
			t.Fatal(err)
		}
		next = v.RenewedBy
	}
	r.passes(10 * time.Minute)
	r.await(a, next, app.AllocUntil{Kind: app.UntilRunning})
	r.await(a, first.ID, app.AllocUntil{Kind: app.UntilEnded})
	if n := len(r.cluster.Submitted()); n != 2 {
		t.Fatalf("%d batch jobs submitted after the renewal, want the standing one and its successor", n)
	}
	views, err := a.Allocations(standingCall.Run, true)
	if err != nil {
		t.Fatal(err)
	}
	successors := 0
	for _, v := range views {
		if v.RenewedBy != "" {
			successors++
		}
	}
	if len(views) != 2 || successors != 1 {
		t.Fatalf("allocations %v, want the first and one successor", ids(views))
	}

	if _, err := a.EndAllocation(ctx, standingCall.Run, next, true); err != nil {
		t.Fatalf("EndAllocation: %v", err)
	}
	r.await(a, next, app.AllocUntil{Kind: app.UntilEnded})
	for len(r.cluster.Live()) != 0 {
		r.tick()
	}
	if left := r.cluster.Running(); len(left) != 0 {
		t.Fatalf("processes left on the cluster: %v", left)
	}
}

func ids(vs []app.AllocationView) []alloc.ID {
	var out []alloc.ID
	for _, v := range vs {
		out = append(out, v.ID)
	}
	slices.Sort(out)
	return out
}

func apptainerFacts(total, idle uint32) apptainer.PartitionFacts {
	return apptainer.PartitionFacts{NodesTotal: total, NodesIdle: idle}
}

func awaitJob(t *testing.T, a *app.App, id core.JobID, want core.JobState) core.Job {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	for {
		j, err := a.Job(ctx, standingScope, id)
		if err != nil {
			t.Fatal(err)
		}
		if j.State == want {
			return j
		}
		if j.State.Final() {
			t.Fatalf("job %s ended %s, want %s", id, j.State, want)
		}
		if _, err := a.Wait(ctx, standingScope, core.WaitFor{IDs: []core.JobID{id}}, func(core.Job) error { return nil }); err != nil {
			t.Fatal(err)
		}
	}
}

// The identity the daemon keeps is an owner the apptainer allocator accepts, however many daemons
// make one: the allocator names every batch job after it, and refuses one that is not letters and
// digits, 1 to 32. Before the identity's own pattern was held to that, one longer than 32 could be
// kept and then refused at every start.
func TestTheDaemonsIdentityIsAnOwnerTheAllocatorAccepts(t *testing.T) {
	for range 50 {
		r := newStandingRig(t, standingConfig)
		a, _ := r.start()
		id, err := filestore.DaemonID(production.AllocationsDir(r.indexes, rigMachine, r.runtime), func() string { return "" })
		if err != nil {
			t.Fatal(err)
		}
		if !regexp.MustCompile(`^[A-Za-z0-9]{1,32}$`).MatchString(id) {
			t.Fatalf("the daemon's identity %q is not an owner", id)
		}
		// The batch job of an allocation is named after it.
		v, err := a.RequestAllocation(r.ctx(), standingCall.Run, clusterPlacement, time.Hour)
		if err != nil {
			t.Fatal(err)
		}
		r.await(a, v.ID, app.AllocUntil{Kind: app.UntilRunning})
		if name := r.cluster.Submitted()[0].Name; !strings.Contains(name, id) {
			t.Fatalf("the batch job %q is not named after the identity %q", name, id)
		}
	}
}

// A placement whose kind has no allocator refuses an allocations section at start, naming the
// placement and its kind, instead of keeping allocations it cannot back; the kinds that have one
// start.
func TestAnAllocationsSectionIsRefusedWhereNoAllocatorBacksIt(t *testing.T) {
	section := "[placements.p.allocations]\nmax_held = 1\nmax_for = \"1h\"\n"
	remote := "[placements.p.remote]\nhost = \"host-a\"\nremote_workspace_root = \"/w\"\n[placements.p.slurm]\npartition = \"gpu\"\n"
	for kind, refused := range map[string]bool{"slurm": true, "apptainer": false, "ssh": false} {
		cfg := "[placements.p]\nkind = \"" + kind + "\"\n" + remote + section
		if kind == "ssh" {
			cfg = "[placements.p]\nkind = \"ssh\"\n[placements.p.remote]\nhost = \"host-a\"\nremote_workspace_root = \"/w\"\n" + section
		}
		r := newStandingRig(t, cfg)
		dir, err := runtimedir.Open(shortDir(t), r.process.UID, runtimedir.System())
		if err != nil {
			t.Fatal(err)
		}
		_, err = r.process.Wire(r.op, rigClocks{r.clock, r.timers}, dir, t.TempDir())
		switch {
		case refused && (err == nil || !strings.Contains(err.Error(), "placements.p") || !strings.Contains(err.Error(), "PLACEMENT_KIND_SLURM")):
			t.Errorf("%s: Wire = %v, want a refusal naming placements.p and its kind", kind, err)
		case !refused && err != nil:
			t.Errorf("%s: Wire = %v, want none", kind, err)
		}
	}
}
