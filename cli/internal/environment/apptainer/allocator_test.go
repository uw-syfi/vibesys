package apptainer_test

import (
	"bytes"
	"context"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/allocatortest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

var standingSizing = apptainer.Sizing{Partition: "part-a", Arguments: []string{"--account=acct-a"}}

// newAllocator is an Allocator of the placement's scope over one process's view of the cluster.
func newAllocator(t testing.TB, cl apptainer.Cluster) *apptainer.Allocator {
	return newOwned(t, cl, "daemon1")
}

func newOwned(t testing.TB, cl apptainer.Cluster, owner string) *apptainer.Allocator {
	t.Helper()
	a, err := apptainer.NewAllocator(apptainer.AllocatorConfig{Cluster: cl, Owner: owner, Scope: scope, Sizing: standingSizing, MaxTime: 48 * time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	return a
}

func spec(n int, run string, d time.Duration) alloc.Spec {
	return alloc.Spec{ID: alloc.ID(fmt.Sprintf("a%d", n)), Run: core.RunID(run), Placement: "p", For: d}
}

// cutter makes every call of the cluster fail as a login node that cannot be reached does.
func cut(c *clusterfake.Cluster, unreachable bool) {
	if !unreachable {
		c.SetFaults(nil)
		return
	}
	c.SetFaults(func(string) error {
		return &core.Failure{Kind: core.FailureUnavailable, Message: "host-a: ssh: connection refused"}
	})
}

func contractWorld(queued int) func(allocatortest.TB) allocatortest.World {
	return func(allocatortest.TB) allocatortest.World {
		w := fakeworld.New()
		cluster := clusterfake.New(w.MemoryGates)
		cluster.Queue(queued, false)
		client := cluster.Connect()
		a, err := apptainer.NewAllocator(apptainer.AllocatorConfig{Cluster: client, Owner: "daemon1", Scope: scope, Sizing: standingSizing})
		if err != nil {
			panic(err)
		}
		return allocatortest.World{
			Allocator:     a,
			Pass:          cluster.Advance,
			Cut:           func(u bool) { cut(cluster, u) },
			CancelOutside: func(h alloc.Handle) { cluster.CancelOutside(h.Ref) },
			Foreign: func() {
				// What is not a standing allocation: a container's, which a sweep owns, a reusable one,
				// and a job of the user's own, all in the scope.
				cluster.Plant(apptainer.JobName("vibesys-cli-foreign"), scope)
				cluster.Plant(apptainer.PoolPrefix+"vibesys-cli-foreign", scope)
				cluster.Plant("someone-elses-job", scope)
			},
		}
	}
}

// The apptainer allocator keeps the contract every allocator keeps, whether the cluster starts an
// allocation at once or after it waits in Slurm's queue.
func TestTheApptainerAllocatorKeepsTheContract(t *testing.T) {
	for name, queued := range map[string]int{"starting at once": 0, "waiting in Slurm's queue": 3} {
		t.Run(name, func(t *testing.T) { allocatortest.Run(t, allocatortest.Contract{New: contractWorld(queued)}) })
	}
}

// Asking twice for one ID, by one process or by one that took over, submits once, and what is
// submitted is a standing allocation: named by the ID, commented with the run, never reusable, and
// no longer than asked for, in whole minutes.
func TestRequestingTheSameIDTwiceSubmitsOnce(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	first := newAllocator(t, cluster.Connect())
	h, err := first.Request(context.Background(), spec(7, "run-a", 90*time.Minute+30*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	again, err := first.Request(context.Background(), spec(7, "run-a", 90*time.Minute+30*time.Second))
	if err != nil || again != h {
		t.Fatalf("a second request: %+v, %v; want %+v", again, err, h)
	}
	// A daemon that restarted asks again, and finds it.
	took, err := newAllocator(t, cluster.Connect()).Request(context.Background(), spec(7, "run-a", 90*time.Minute+30*time.Second))
	if err != nil || took != h {
		t.Fatalf("a request after a restart: %+v, %v; want %+v", took, err, h)
	}
	sub := cluster.Submitted()
	if len(sub) != 1 {
		t.Fatalf("%d allocations submitted, want one: %+v", len(sub), sub)
	}
	want := apptainer.Allocation{
		Name: "vibesys-apptainer-standing-daemon1-a7", Comment: apptainer.RunComment(scope, "run-a"), Limit: 90 * time.Minute,
		Options: standingSizing.Options(true),
	}
	if got := sub[0]; got.Name != want.Name || got.Comment != want.Comment || got.Limit != want.Limit || got.Idle != 0 || !slices.Equal(got.Options, want.Options) {
		t.Errorf("submitted %+v, want %+v", got, want)
	}
	// Another ID is another allocation.
	if _, err := first.Request(context.Background(), spec(8, "run-a", time.Hour)); err != nil {
		t.Fatal(err)
	}
	if n := len(cluster.Submitted()); n != 2 {
		t.Errorf("%d allocations after a request of another ID, want 2", n)
	}
}

// A request the cluster could not take is refused as invalid input, and nothing is submitted.
func TestARequestThatCannotBeTakenSubmitsNothing(t *testing.T) {
	cluster := clusterfake.New(fakeworld.New().MemoryGates)
	a := newAllocator(t, cluster.Connect())
	for _, s := range []alloc.Spec{
		spec(1, "run-a", 30*time.Second), spec(1, "run-a", 49*time.Hour), spec(1, "run a", time.Hour), spec(1, "", time.Hour),
		{ID: "x", Run: "run-a", For: time.Hour},
	} {
		_, err := a.Request(context.Background(), s)
		if f := core.AsFailure(err); err == nil || f.Kind != core.FailureInvalidInput {
			t.Errorf("Request(%+v) = %v, want invalid input", s, err)
		}
	}
	if n := len(cluster.Submitted()); n != 0 {
		t.Errorf("%d allocations submitted by refused requests", n)
	}
}

// The process that supervised a standing allocation dying does not cancel it, as it does the
// allocation of a container: the time limit is its only backstop.
func TestASupervisorExitDoesNotCancelAStandingAllocation(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	supervisor := cluster.Connect()
	a := newAllocator(t, supervisor)
	h, err := a.Request(context.Background(), spec(1, "run-a", 2*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	// A container of the same process whose allocation is its own is held, and dies with it.
	r := newRig(t, w, cluster, setup{})
	if _, err := r.env.Fingerprint(context.Background()); err != nil {
		t.Fatal(err)
	}
	own, err := r.engine.Start(context.Background(), docker.ContainerSpec{
		Name: "vibesys-cli-own", Image: mustFingerprint(t, r), Dir: "/work", Limit: limit, Labels: docker.Labels{docker.LabelScope: scope},
	})
	if err != nil {
		t.Fatal(err)
	}
	_ = own
	supervisor.Crash()
	r.client.Crash()
	if live := cluster.Live(); !slices.Equal(live, []string{"vibesys-apptainer-standing-daemon1-a1"}) {
		t.Fatalf("live after the supervisor died: %v, want the standing allocation alone", live)
	}
	// The next process finds it running, and adopts it.
	next := newAllocator(t, cluster.Connect())
	if o, err := next.Observe(context.Background(), h); err != nil || !isRunning(o) {
		t.Fatalf("observed %+v, %v; want running", o, err)
	}
	if got, err := next.Adopt(context.Background()); err != nil || !slices.Equal(got, []alloc.Handle{h}) {
		t.Fatalf("adopted %+v, %v; want %+v", got, err, h)
	}
	// Only Slurm's time limit ends it.
	cluster.Advance(2*time.Hour - time.Minute)
	if o, _ := next.Observe(context.Background(), h); !isRunning(o) {
		t.Fatalf("a minute before its limit: %+v", o)
	}
	cluster.Advance(time.Minute)
	if o, _ := next.Observe(context.Background(), h); o != (alloc.SeenEnded{Cause: alloc.EndedByTimeLimit, At: o.(alloc.SeenEnded).At}) {
		t.Fatalf("at its limit: %+v", o)
	}
}

func mustFingerprint(t testing.TB, r *rig) string {
	t.Helper()
	f, err := r.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return f.ImageDigest
}

func isRunning(o alloc.Observation) bool {
	_, ok := o.(alloc.SeenRunning)
	return ok
}

// Regression (invariant 4): a run's sweep removes what a dead run of its scope left, and a standing
// allocation is not a container's leftover: it is never removed, whether the run that owns it lives
// or is dead, however the placement scopes its runs.
func TestASweepNeverCancelsAStandingAllocation(t *testing.T) {
	for name, withRuns := range map[string]bool{"a placement of jobs at once": true, "one run at a time": false} {
		t.Run(name, func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			table := dockerfake.NewRunTable()
			live := table.Process()
			if err := live.Hold(context.Background(), "run-live"); err != nil {
				t.Fatal(err)
			}
			a := newAllocator(t, cluster.Connect())
			for i, run := range []string{"run-live", "run-dead", "run-self", "run-other"} {
				if _, err := a.Request(context.Background(), spec(i+1, run, time.Hour)); err != nil {
					t.Fatal(err)
				}
			}
			// A container's own leftover of the dead run, which the sweep does remove.
			leftover := apptainer.JobName("vibesys-cli-dead000001")
			cluster.Plant(leftover, apptainer.RunComment(scope, "run-dead"))
			o := setup{}
			if withRuns {
				o = setup{runs: table.Process(), run: "run-self"}
			}
			r := newRig(t, w, cluster, o)
			at, _ := run(t, r.env, "exit", "0")
			exited(t, at, 0)
			want := []string{"vibesys-apptainer-standing-daemon1-a1", "vibesys-apptainer-standing-daemon1-a2", "vibesys-apptainer-standing-daemon1-a3", "vibesys-apptainer-standing-daemon1-a4"}
			if got := cluster.Live(); !slices.Equal(got, want) {
				t.Errorf("live allocations %v after a run's sweep, want only the four standing ones", got)
			}
		})
	}
}

// A standing allocation is released by its Allocator alone: the engine refuses to remove it as a
// container.
func TestTheEngineDoesNotRemoveAStandingAllocationAsAContainer(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	if _, err := newAllocator(t, cluster.Connect()).Request(context.Background(), spec(1, "run-a", time.Hour)); err != nil {
		t.Fatal(err)
	}
	r := newRig(t, w, cluster, setup{})
	err := r.engine.Remove(context.Background(), "standing-daemon1-a1")
	if f := core.AsFailure(err); err == nil || f.Kind != core.FailureInvalidInput {
		t.Fatalf("Remove = %v, want invalid input", err)
	}
	if live := cluster.Live(); len(live) != 1 {
		t.Errorf("live %v", live)
	}
}

// startIn starts a container of the job in the standing allocation.
func startIn(t testing.TB, r *rig, name, ref, network string) docker.Container {
	t.Helper()
	labels := docker.Labels{docker.LabelScope: scope}
	if ref != "" {
		labels[apptainer.LabelAllocation] = ref
	}
	c, err := r.engine.Start(context.Background(), docker.ContainerSpec{
		Name: name, Image: mustFingerprint(t, r), Dir: "/work", Limit: limit, Network: network, Labels: labels,
	})
	if err != nil {
		t.Fatalf("starting %s: %v", name, err)
	}
	return c
}

// A job's first container joins the standing allocation, as a second container joins the first's,
// and its second joins it too: both run on the allocation's node and ask for no allocation of their
// own. When the job ends its processes are killed, confirmed, and the allocation runs on, ready for
// the next job.
func TestAJobJoiningAStandingAllocationRunsThereAndLeavesItRunning(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	h, err := newAllocator(t, cluster.Connect()).Request(context.Background(), spec(1, "run-a", 4*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	r := newRig(t, w, cluster, setup{})
	const first, second = "vibesys-cli-first", "vibesys-cli-second"
	for job := range 2 {
		c1 := startIn(t, r, first, h.Ref, "")
		c2 := startIn(t, r, second, "", "container:"+first)
		var out bytes.Buffer
		p, err := c1.Start([]string{"print", fmt.Sprint("job ", job)}, &out)
		if err != nil {
			t.Fatal(err)
		}
		if e := <-p.Exited(); e.Code != 0 || !strings.Contains(out.String(), fmt.Sprint("job ", job)) {
			t.Fatalf("exit %+v, output %q", e, out.String())
		}
		// A server that holds on until the job ends, in the second container.
		if _, err := c2.Start([]string{"hold", "forever"}, &bytes.Buffer{}); err != nil {
			t.Fatal(err)
		}
		if running := cluster.Running(); len(running) == 0 {
			t.Fatal("nothing runs in the standing allocation")
		}
		for _, name := range []string{second, first} {
			c := map[string]docker.Container{first: c1, second: c2}[name]
			c.Close()
			if err := r.engine.Remove(context.Background(), name); err != nil {
				t.Fatalf("removing %s: %v", name, err)
			}
		}
		if running := cluster.Running(); len(running) != 0 {
			t.Fatalf("processes left after the job ended: %v", running)
		}
		if live := cluster.Live(); !slices.Equal(live, []string{"vibesys-apptainer-standing-daemon1-a1"}) {
			t.Fatalf("live after the job ended: %v, want the standing allocation, still running", live)
		}
	}
	if n := len(cluster.Submitted()); n != 1 {
		t.Errorf("%d allocations submitted, want the standing one alone", n)
	}
	for _, b := range cluster.Started() {
		if b.Name != first && b.Name != second {
			t.Errorf("a box %s ran", b.Name)
		}
	}
}

// A container that names a standing allocation that does not run is refused, and nothing is
// submitted or cancelled.
func TestAContainerIsRefusedAStandingAllocationThatDoesNotRun(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	a := newAllocator(t, cluster.Connect())
	h, err := a.Request(context.Background(), spec(1, "run-a", time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	cluster.CancelOutside(h.Ref)
	r := newRig(t, w, cluster, setup{})
	_, err = r.engine.Start(context.Background(), docker.ContainerSpec{
		Name: "vibesys-cli-x", Image: mustFingerprint(t, r), Dir: "/work", Limit: limit,
		Labels: docker.Labels{docker.LabelScope: scope, apptainer.LabelAllocation: h.Ref},
	})
	if f := core.AsFailure(err); err == nil || f.Kind != core.FailureUnavailable {
		t.Fatalf("Start = %v, want unavailable", err)
	}
	if n := len(cluster.Submitted()); n != 1 {
		t.Errorf("%d allocations submitted, want the standing one alone", n)
	}
}

// While the login node cannot be reached an observation fails as unavailable, never as an end, and
// once it answers again the allocation, whatever happened meanwhile, is as the cluster says.
func TestAnOutageDuringObserveIsUnavailableNotAnEnd(t *testing.T) {
	cluster := clusterfake.New(fakeworld.New().MemoryGates)
	a := newAllocator(t, cluster.Connect())
	h, err := a.Request(context.Background(), spec(1, "run-a", time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	cut(cluster, true)
	for range 3 {
		o, err := a.Observe(context.Background(), h)
		if f := core.AsFailure(err); o != nil || err == nil || f.Kind != core.FailureUnavailable {
			t.Fatalf("Observe = %+v, %v; want unavailable", o, err)
		}
	}
	// The limit passes while nobody can ask: the first answer afterwards says so.
	cluster.Advance(2 * time.Hour)
	cut(cluster, false)
	o, err := a.Observe(context.Background(), h)
	if err != nil || o.(alloc.SeenEnded).Cause != alloc.EndedByTimeLimit {
		t.Fatalf("Observe = %+v, %v; want ended by its limit", o, err)
	}
}

// Slurm's time limit ends an allocation by TIMEOUT, and a cancel by anyone else, or a node's
// failure, is an end that is not the limit's.
func TestHowAnAllocationEndedIsWhatSlurmSays(t *testing.T) {
	for name, c := range map[string]struct {
		end  func(*clusterfake.Cluster, alloc.Handle)
		want alloc.EndCause
	}{
		"its time limit":       {func(c *clusterfake.Cluster, _ alloc.Handle) { c.Advance(time.Hour) }, alloc.EndedByTimeLimit},
		"a cancel by someone":  {func(c *clusterfake.Cluster, h alloc.Handle) { c.CancelOutside(h.Ref) }, alloc.EndedOtherwise},
		"a failure of a node":  {func(c *clusterfake.Cluster, h alloc.Handle) { c.FailNode(h.Ref) }, alloc.EndedOtherwise},
		"a release of the CLI": {func(c *clusterfake.Cluster, h alloc.Handle) { _ = c.Connect().CancelJob(context.Background(), h.Ref) }, alloc.EndedOtherwise},
	} {
		t.Run(name, func(t *testing.T) {
			cluster := clusterfake.New(fakeworld.New().MemoryGates)
			a := newAllocator(t, cluster.Connect())
			h, err := a.Request(context.Background(), spec(1, "run-a", time.Hour))
			if err != nil {
				t.Fatal(err)
			}
			if o, _ := a.Observe(context.Background(), h); !isRunning(o) {
				t.Fatalf("before the end: %+v", o)
			}
			c.end(cluster, h)
			o, err := a.Observe(context.Background(), h)
			if got, ok := o.(alloc.SeenEnded); err != nil || !ok || got.Cause != c.want {
				t.Fatalf("Observe = %+v, %v; want ended, cause %s", o, err, c.want)
			}
		})
	}
}

// A queued allocation says why it waits and when Slurm expects it to start.
func TestAQueuedAllocationSaysWhyItWaits(t *testing.T) {
	cluster := clusterfake.New(fakeworld.New().MemoryGates)
	cluster.Queue(5, false)
	a := newAllocator(t, cluster.Connect())
	h, _ := a.Request(context.Background(), spec(1, "run-a", time.Hour))
	o, err := a.Observe(context.Background(), h)
	p, ok := o.(alloc.SeenPending)
	if err != nil || !ok || p.Reason != "Priority" || !p.EstimatedStart.After(clusterfake.Epoch) {
		t.Fatalf("Observe = %+v, %v; want pending, with its reason and an estimate", o, err)
	}
}

// Random runs of requests, looks, releases, outages, outside cancels, node failures, the passage of
// time and sweeps keep the allocator's promises at every step: what it says of an allocation is what
// the cluster holds, an outage is never an end, an ended allocation stays ended, Adopt is exactly the
// live standing ones, one ID is one submission, and no sweep cancels one.
func TestRandomLivesOfStandingAllocationsKeepTheContract(t *testing.T) {
	ctx := context.Background()
	for seed := uint64(1); seed <= 60; seed++ {
		t.Run(fmt.Sprint("seed ", seed), func(t *testing.T) {
			rng := rand.New(rand.NewPCG(seed, seed*7919))
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			cluster.Queue(rng.IntN(3), false)
			a := newAllocator(t, cluster.Connect())
			r := newRig(t, w, cluster, setup{})
			handles := map[int]alloc.Handle{}
			other := newOwned(t, cluster.Connect(), "daemon2") // another daemon of the same scope
			theirs := map[int]alloc.Handle{}
			over := map[int]bool{} // seen ended or released
			cutNow := false
			const ids = 5
			truth := func(h alloc.Handle) apptainer.Phase {
				_, _, p, _ := cluster.Job(h.Ref)
				return p
			}
			for step := range 60 {
				id := 1 + rng.IntN(ids)
				h, have := handles[id]
				switch op := rng.IntN(12); op {
				case 0, 1: // request, again if it is live
					if have && truth(h) == apptainer.Ended {
						continue
					}
					got, err := a.Request(ctx, spec(id, fmt.Sprint("run-", id%2), time.Duration(1+rng.IntN(4))*time.Hour))
					if cutNow {
						if core.AsFailure(err).Kind != core.FailureUnavailable {
							t.Fatalf("step %d: Request while cut = %v", step, err)
						}
						continue
					}
					if err != nil {
						t.Fatalf("step %d: Request: %v", step, err)
					}
					if have && got != h {
						t.Fatalf("step %d: Request of a live allocation made %+v, want %+v", step, got, h)
					}
					handles[id] = got
				case 2, 3: // look
					if !have {
						continue
					}
					o, err := a.Observe(ctx, h)
					if cutNow {
						if o != nil || core.AsFailure(err).Kind != core.FailureUnavailable {
							t.Fatalf("step %d: Observe while cut = %+v, %v", step, o, err)
						}
						continue
					}
					if err != nil {
						t.Fatalf("step %d: Observe: %v", step, err)
					}
					_, ended := o.(alloc.SeenEnded)
					if want := truth(h) == apptainer.Ended; ended != want {
						t.Fatalf("step %d: Observe = %+v, the cluster says ended=%v", step, o, want)
					}
					if over[id] && !ended {
						t.Fatalf("step %d: an ended allocation is %+v again", step, o)
					}
					over[id] = over[id] || ended
				case 4: // release
					if !have {
						continue
					}
					err := a.Release(ctx, h)
					if cutNow {
						if core.AsFailure(err).Kind != core.FailureUnavailable {
							t.Fatalf("step %d: Release while cut = %v", step, err)
						}
						continue
					}
					if err != nil || truth(h) != apptainer.Ended {
						t.Fatalf("step %d: Release = %v, the cluster says %v", step, err, truth(h))
					}
					over[id] = true
				case 5:
					cluster.Advance(time.Duration(rng.IntN(90)) * time.Minute)
				case 6:
					if have {
						cluster.CancelOutside(h.Ref)
					}
				case 7:
					if have {
						cluster.FailNode(h.Ref)
					}
				case 8:
					cutNow = !cutNow
					cut(cluster, cutNow)
				case 10: // the other daemon requests, under the same IDs
					if cutNow {
						continue
					}
					if oh, ok := theirs[id]; ok && truth(oh) == apptainer.Ended {
						continue
					}
					oh, err := other.Request(ctx, spec(id, "run-x", time.Hour))
					if err != nil {
						t.Fatalf("step %d: the other daemon's Request: %v", step, err)
					}
					if prev, ok := theirs[id]; ok && prev != oh {
						t.Fatalf("step %d: the other daemon's Request made %+v, want %+v", step, oh, prev)
					}
					if mine, ok := handles[id]; ok && mine.Ref == oh.Ref {
						t.Fatalf("step %d: two daemons share the allocation %+v", step, oh)
					}
					theirs[id] = oh
				case 11: // the other daemon releases one of its own
					if oh, ok := theirs[id]; ok && !cutNow {
						if err := other.Release(ctx, oh); err != nil {
							t.Fatalf("step %d: the other daemon's Release: %v", step, err)
						}
					}
				case 9: // a run's sweep
					if cutNow {
						continue
					}
					before := standingLive(cluster)
					names, err := r.engine.Containers(ctx, docker.Labels{docker.LabelScope: scope})
					if err != nil {
						t.Fatalf("step %d: sweep: %v", step, err)
					}
					for _, n := range names {
						if err := r.engine.Remove(ctx, n); err != nil {
							t.Fatalf("step %d: sweep removing %s: %v", step, n, err)
						}
					}
					if after := standingLive(cluster); !slices.Equal(before, after) {
						t.Fatalf("step %d: a sweep changed the standing allocations from %v to %v", step, before, after)
					}
				}
				if cutNow {
					continue
				}
				for who, c := range map[string]struct {
					a  *apptainer.Allocator
					hs map[int]alloc.Handle
				}{"this daemon": {a, handles}, "the other daemon": {other, theirs}} {
					got, err := c.a.Adopt(ctx)
					if err != nil {
						t.Fatalf("step %d: %s: Adopt: %v", step, who, err)
					}
					var want []alloc.Handle
					for _, hh := range c.hs {
						if truth(hh) != apptainer.Ended {
							want = append(want, hh)
						}
					}
					byID := func(x, y alloc.Handle) int { return strings.Compare(string(x.Alloc), string(y.Alloc)) }
					slices.SortFunc(got, byID)
					slices.SortFunc(want, byID)
					if !slices.Equal(got, want) {
						t.Fatalf("step %d: %s: Adopt = %+v, want exactly its own live ones, %+v", step, who, got, want)
					}
				}
			}
			if n := len(cluster.Submitted()); n != len(handles)+len(theirs) {
				t.Fatalf("%d submissions for %d IDs", n, len(handles))
			}
		})
	}
}

func standingLive(c *clusterfake.Cluster) []string {
	return slices.DeleteFunc(c.Live(), func(n string) bool { return !strings.HasPrefix(n, apptainer.StandingPrefix) })
}

// Two daemons of one scope, each with an owner of its own, never find, release or sweep one
// another's allocations, though they ask for the same IDs.
func TestDaemonsOfOneScopeNeverTakeEachOthersAllocations(t *testing.T) {
	ctx := context.Background()
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	a, b := newOwned(t, cluster.Connect(), "daemon1"), newOwned(t, cluster.Connect(), "daemon2")
	ha, err := a.Request(ctx, spec(1, "run-a", time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	hb, err := b.Request(ctx, spec(1, "run-a", time.Hour))
	if err != nil || hb == ha {
		t.Fatalf("the same ID of two owners: %+v and %+v, %v; want two allocations", ha, hb, err)
	}
	if n := len(cluster.Submitted()); n != 2 {
		t.Fatalf("%d submissions, want 2", n)
	}
	for who, c := range map[string]struct {
		a *apptainer.Allocator
		h alloc.Handle
	}{"a": {a, ha}, "b": {b, hb}} {
		if got, err := c.a.Adopt(ctx); err != nil || !slices.Equal(got, []alloc.Handle{c.h}) {
			t.Errorf("%s adopted %+v, %v; want only %+v", who, got, err, c.h)
		}
	}
	// A sweep of this scope, and a release by one, leave the other's.
	r := newRig(t, w, cluster, setup{})
	at, _ := run(t, r.env, "exit", "0")
	exited(t, at, 0)
	if err := a.Release(ctx, ha); err != nil {
		t.Fatal(err)
	}
	if got, _ := b.Adopt(ctx); !slices.Equal(got, []alloc.Handle{hb}) {
		t.Errorf("b adopted %+v after a's release and a sweep, want %+v", got, hb)
	}
	if o, err := b.Observe(ctx, hb); err != nil || !isRunning(o) {
		t.Errorf("b's allocation is %+v, %v", o, err)
	}
	for _, owner := range []string{"", "a-b", "a b", strings.Repeat("x", 33)} {
		if _, err := apptainer.NewAllocator(apptainer.AllocatorConfig{Cluster: cluster.Connect(), Owner: owner, Scope: scope}); err == nil {
			t.Errorf("owner %q was accepted", owner)
		}
	}
}
