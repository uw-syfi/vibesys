package sim_test

import (
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/production"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
)

// Two daemons whose state home is one directory (a home shared between machines, or two runtime
// directories on one machine) keep their allocations apart over one cluster: each has an identity of
// its own, names its batch jobs after it, writes a log of its own, and neither adopts nor releases
// the other's, even when both serve a run of one name and number their allocations alike, and across
// a restart of either. Before the log and identity were keyed by the daemon, both daemons read one
// identity, so the second daemon's request found the first one's batch job and took it as its own,
// and both appended to one log.
func TestTwoDaemonsOnASharedStateHomeKeepTheirAllocationsApart(t *testing.T) {
	daemons := map[string]struct{ machine, other string }{
		"two machines":                         {machine: "host-b", other: rigMachine},
		"one machine, two runtime directories": {machine: rigMachine, other: rigMachine},
	}
	for name, d := range daemons {
		t.Run(name, func(t *testing.T) {
			r := newStandingRig(t, standingConfig)
			second := "/run/user/1000/vibesys-cli-2"
			a, _ := r.startAs(d.other, r.runtime)
			timersA := r.timers
			b, _ := r.startAs(d.machine, second)
			timersB := r.timers
			on := func(timers *observeTimers) { r.timers = timers }

			va, err := a.RequestAllocation(r.ctx(), standingCall.Run, clusterPlacement, time.Hour)
			if err != nil {
				t.Fatalf("RequestAllocation on the first daemon: %v", err)
			}
			vb, err := b.RequestAllocation(r.ctx(), standingCall.Run, clusterPlacement, time.Hour)
			if err != nil {
				t.Fatalf("RequestAllocation on the second daemon: %v", err)
			}
			on(timersA)
			ra := r.await(a, va.ID, app.AllocUntil{Kind: app.UntilRunning})
			on(timersB)
			rb := r.await(b, vb.ID, app.AllocUntil{Kind: app.UntilRunning})

			submitted := r.cluster.Submitted()
			if len(submitted) != 2 || submitted[0].Name == submitted[1].Name {
				t.Fatalf("batch jobs submitted: %+v, want one of each daemon, with names of their own", submitted)
			}
			if ra.Handle.Ref == rb.Handle.Ref {
				t.Fatalf("both daemons hold batch job %s", ra.Handle.Ref)
			}

			// Each daemon looks at the cluster a few times, and a restart of the first adopts again:
			// neither releases, nor lists, the other's.
			for range 3 {
				on(timersA)
				r.tick()
				on(timersB)
				r.tick()
			}
			a.Close()
			a, _ = r.startAs(d.other, r.runtime)
			timersA = r.timers
			for range 3 {
				on(timersA)
				r.tick()
			}
			for _, ref := range []string{ra.Handle.Ref, rb.Handle.Ref} {
				if _, _, phase, ok := r.cluster.Job(ref); !ok || phase == apptainer.Ended {
					t.Fatalf("batch job %s ended while both daemons held their own", ref)
				}
			}
			for _, c := range []struct {
				who  *app.App
				want app.AllocationView
			}{{a, ra}, {b, rb}} {
				all, err := c.who.Allocations("", true)
				if err != nil {
					t.Fatal(err)
				}
				if len(all) != 1 || all[0].ID != c.want.ID || all[0].Handle != c.want.Handle || all[0].Stage() != alloc.StageRunning {
					t.Fatalf("a daemon lists %+v, want its own allocation %s on %s alone, running", all, c.want.ID, c.want.Handle.Ref)
				}
			}
			// Each keeps an identity of its own.
			idA, errA := filestore.DaemonID(production.AllocationsDir(r.indexes, d.other, r.runtime), func() string { return "" })
			idB, errB := filestore.DaemonID(production.AllocationsDir(r.indexes, d.machine, second), func() string { return "" })
			if errA != nil || errB != nil || idA == idB {
				t.Fatalf("the daemons' identities are %q (%v) and %q (%v), want two", idA, errA, idB, errB)
			}
		})
	}
}
