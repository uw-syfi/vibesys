package apptainer_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

const idle = 30 * time.Minute

// runMeasured runs the command as a run of a check or, measured, of a benchmark sample.
func runMeasured(t testing.TB, r *rig, measured bool, argv ...string) core.Attempt {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	var out bytes.Buffer
	return r.env.Run(ctx, core.Execution{Checkout: checkout(t), Argv: argv, Timeout: limit, Measured: measured}, &out)
}

// pools are the reusable allocations live on the cluster.
func pools(c *clusterfake.Cluster) []string {
	return slices.DeleteFunc(c.Live(), func(n string) bool { return !strings.HasPrefix(n, apptainer.PoolPrefix) })
}

// With reuse, a check and a measure's samples, and the runs of a later process of the scope, share
// one allocation, asked for once, for the whole node, with the allocation limit and the idle bound:
// each run's processes are gone, and the claim released, when it ends, and the allocation waits for
// the next. It ends once idle for its bound, and the next run asks for a new one.
func TestAReusedAllocationServesACheckAndEverySample(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	setupOf := func(process uint32) setup {
		return setup{idle: idle, process: process}
	}
	r := newRig(t, w, cluster, setupOf(0))
	for i, measured := range []bool{false, true, true, true, true, true} {
		exited(t, runMeasured(t, r, measured, "print", fmt.Sprint(i)), 0)
		if running, claimed := cluster.Running(), cluster.Claimed(); len(running) != 0 || len(claimed) != 0 {
			t.Fatalf("after run %d: processes %v and claims %v left", i, running, claimed)
		}
	}
	sub := cluster.Submitted()
	if len(sub) != 1 {
		t.Fatalf("allocations asked for %+v, want one", sub)
	}
	if a := sub[0]; !strings.HasPrefix(a.Name, apptainer.PoolPrefix) || a.Idle != idle || a.Limit != apptainer.NoLimit ||
		!slices.Equal(a.Options, (apptainer.Sizing{Partition: "part-a", Arguments: []string{"--account=acct-a"}}).Options(true)) {
		t.Errorf("allocation %+v, want a reusable one of the measured size", a)
	}
	// A later process of the scope, whose run sweeps what earlier ones left, claims it too.
	exited(t, runMeasured(t, newRig(t, w, cluster, setupOf(1)), true, "exit", "0"), 0)
	if n := len(cluster.Submitted()); n != 1 || len(pools(cluster)) != 1 {
		t.Errorf("a later process asked for %d allocations; live %v", n, cluster.Live())
	}
	cluster.Advance(idle - time.Second)
	if len(pools(cluster)) != 1 {
		t.Fatal("the allocation ended before it was idle for its bound")
	}
	cluster.Advance(time.Second)
	if live := cluster.Live(); len(live) != 0 {
		t.Fatalf("an allocation idle for its bound is live: %v", live)
	}
	exited(t, runMeasured(t, newRig(t, w, cluster, setupOf(2)), true, "exit", "0"), 0)
	if n := len(cluster.Submitted()); n != 2 {
		t.Errorf("after the first ended, %d allocations asked for, want 2", n)
	}
}

// An allocation is claimed only with the container's limit and the stop's margin left: one with
// less is never claimed, and a new one is asked for.
func TestAnAllocationWithTooLittleTimeLeftIsNotReused(t *testing.T) {
	const allocation = 2 * time.Hour
	need := limit + apptainer.StopMargin
	for _, elapsed := range []time.Duration{0, allocation - need - time.Minute, allocation - need, allocation - need + time.Second, allocation - need + 10*time.Minute} {
		t.Run(elapsed.String(), func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			r := newRig(t, w, cluster, setup{idle: 3 * time.Hour, allocationLimit: allocation})
			exited(t, runMeasured(t, r, true, "exit", "0"), 0)
			cluster.Advance(elapsed)
			exited(t, runMeasured(t, r, true, "exit", "0"), 0)
			sub := cluster.Submitted()
			if reused := len(sub) == 1; reused != (allocation-elapsed >= need) {
				t.Errorf("with %v left of %v, %d allocations asked for", allocation-elapsed, allocation, len(sub))
			}
			if sub[0].Limit != allocation {
				t.Errorf("the allocation's limit %v, want %v", sub[0].Limit, allocation)
			}
		})
	}
}

// Whatever containers start and are removed, in any order, each live one holds a claim of an
// allocation of its own, so none runs beside another (a measured one among them), and an
// allocation is asked for only when every live one is claimed: there are never more than the most
// containers ever live at once.
func TestNoTwoContainersClaimOneAllocation(t *testing.T) {
	err := quick.Check(func(seed uint64) bool {
		rnd := rand.New(rand.NewPCG(seed, 3))
		w := fakeworld.New()
		cluster := clusterfake.New(w.MemoryGates)
		r := newRig(t, w, cluster, setup{idle: idle})
		f, err := r.env.Fingerprint(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		ctx := context.Background()
		live := map[string]docker.Container{}
		most := 0
		for i := range 12 {
			name := fmt.Sprintf("vibesys-cli-box%d", rnd.IntN(4))
			if c, ok := live[name]; ok {
				c.Close()
				if err := r.engine.Remove(ctx, name); err != nil {
					t.Fatal(err)
				}
				delete(live, name)
			} else {
				c, err := r.engine.Start(ctx, docker.ContainerSpec{
					Name: name, Image: f.ImageDigest, Dir: "/work", Limit: limit, Measured: rnd.IntN(2) == 0,
					Labels: docker.Labels{docker.LabelScope: scope},
				})
				if err != nil {
					t.Fatal(err)
				}
				if _, err := c.Start([]string{"hold", "forever"}, &bytes.Buffer{}); err != nil {
					t.Fatal(err)
				}
				live[name] = c
			}
			most = max(most, len(live))
			if claimed := cluster.Claimed(); len(claimed) != len(live) {
				t.Logf("step %d: %d containers live, claims of %v", i, len(live), claimed)
				return false
			}
			if n := len(cluster.Submitted()); n != most {
				t.Logf("step %d: %d allocations asked for, and at most %d containers were live", i, n, most)
				return false
			}
		}
		for name, c := range live {
			c.Close()
			if err := r.engine.Remove(ctx, name); err != nil {
				t.Fatal(err)
			}
		}
		return len(cluster.Running()) == 0 && len(cluster.Claimed()) == 0
	}, &quick.Config{MaxCount: 30})
	if err != nil {
		t.Error(err)
	}
}

// The processes a dead process's container left in a reusable allocation, before the allocation
// noticed the claim was dead, are killed before the next container that claims it runs anything,
// and the claim of the dead one does not keep it.
func TestWhatADeadClaimLeftIsGoneBeforeTheNextContainerRuns(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	cluster.Linger(true)
	dead := newRig(t, w, cluster, setup{idle: idle})
	f, err := dead.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	c, err := dead.engine.Start(context.Background(), docker.ContainerSpec{
		Name: "vibesys-cli-dead", Image: f.ImageDigest, Dir: "/work", Limit: limit, Labels: docker.Labels{docker.LabelScope: scope},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Start([]string{"hold", "forever"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	dead.client.Crash()
	if running := cluster.Running(); len(running) != 1 {
		t.Fatalf("the dead process's container: %v", running)
	}
	next := newRig(t, w, cluster, setup{idle: idle, process: 1})
	// Each process's Docker builds the image of its own.
	if _, err := next.env.Fingerprint(context.Background()); err != nil {
		t.Fatal(err)
	}
	c2, err := next.engine.Start(context.Background(), docker.ContainerSpec{
		Name: "vibesys-cli-next", Image: f.ImageDigest, Dir: "/work", Limit: limit, Measured: true, Labels: docker.Labels{docker.LabelScope: scope},
	})
	if err != nil {
		t.Fatal(err)
	}
	if running := cluster.Running(); len(running) != 0 {
		t.Errorf("before the next container ran anything, left: %v", running)
	}
	if n := len(cluster.Submitted()); n != 1 {
		t.Errorf("%d allocations asked for, want the dead one's claimed again", n)
	}
	c2.Close()
	if err := next.engine.Remove(context.Background(), "vibesys-cli-next"); err != nil {
		t.Fatal(err)
	}
}

// A release that is not confirmed cancels the allocation, so nothing of the container is left in
// one another claims; the run's ending is still its command's.
func TestAnUnconfirmedReleaseCancelsTheAllocation(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	r := newRig(t, w, cluster, setup{idle: idle})
	cluster.SetFaults(func(op string) error {
		if op == "release" {
			return &core.Failure{Kind: core.FailureUnavailable, Message: "the claim's step was lost"}
		}
		return nil
	})
	exited(t, runMeasured(t, r, true, "exit", "0"), 0)
	cluster.SetFaults(nil)
	nothingLeft(t, cluster)
}

// startHeld starts a container of the scope on the rig's engine that runs until it is closed.
func startHeld(t testing.TB, r *rig, image, name string) (docker.Container, error) {
	t.Helper()
	c, err := r.engine.Start(context.Background(), docker.ContainerSpec{
		Name: name, Image: image, Dir: "/work", Limit: limit, Measured: true, Labels: docker.Labels{docker.LabelScope: scope},
	})
	if err != nil {
		return nil, err
	}
	if _, err := c.Start([]string{"hold", "forever"}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	return c, nil
}

// A container that asked for a new allocation and waits for it to run can find, once it runs, that
// another container of the placement (of another process) found it running and claimed it first.
// The allocation is then the other's: it is not cancelled under it, and the first container asks
// for another of a name of its own, so that its own cancel never ends the other's either.
func TestAnAllocationAnotherContainerClaimedFirstIsLeftToIt(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	first := newRig(t, w, cluster, setup{idle: idle})
	second := newRig(t, w, cluster, setup{idle: idle, process: 1})
	f, err := first.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// Each process's Docker builds the image of its own.
	if _, err := second.env.Fingerprint(context.Background()); err != nil {
		t.Fatal(err)
	}
	// The first claim the cluster sees is the first container's, of the allocation it asked for,
	// which runs: just before it, the second container starts, and claims that allocation.
	var claims atomic.Int32
	var c2 docker.Container
	var err2 error
	cluster.SetFaults(func(op string) error {
		if op == "claim" && claims.Add(1) == 1 {
			c2, err2 = startHeld(t, second, f.ImageDigest, "vibesys-cli-second")
		}
		return nil
	})
	c1, err := startHeld(t, first, f.ImageDigest, "vibesys-cli-first")
	cluster.SetFaults(nil)
	if err2 != nil {
		t.Fatalf("the second container: %v", err2)
	}
	if err != nil {
		t.Fatalf("the first container, whose allocation the second claimed: %v", err)
	}
	taken := apptainer.PoolPrefix + "vibesys-cli-first"
	sub := cluster.Submitted()
	if len(sub) != 2 || sub[0].Name != taken || sub[1].Name != taken+"+2" {
		t.Fatalf("allocations asked for %+v, want the first's and another of a name of its own", sub)
	}
	if live := pools(cluster); !slices.Contains(live, taken) || !slices.Contains(live, taken+"+2") {
		t.Fatalf("live allocations %v, want both", live)
	}
	if claimed := cluster.Claimed(); len(claimed) != 2 {
		t.Fatalf("claims %v, want one for each container", claimed)
	}
	// A release of the first container that is not confirmed cancels its allocation, and only its.
	cluster.SetFaults(func(op string) error {
		if op == "release" {
			return &core.Failure{Kind: core.FailureUnavailable, Message: "the claim's step was lost"}
		}
		return nil
	})
	c1.Close()
	if err := first.engine.Remove(context.Background(), "vibesys-cli-first"); err != nil {
		t.Fatal(err)
	}
	cluster.SetFaults(nil)
	if live := pools(cluster); !slices.Equal(live, []string{taken}) {
		t.Fatalf("after the first container's allocation was cancelled, live %v, want the second's", live)
	}
	if claimed := cluster.Claimed(); len(claimed) != 1 {
		t.Fatalf("claims %v, want the second container's", claimed)
	}
	c2.Close()
	if err := second.engine.Remove(context.Background(), "vibesys-cli-second"); err != nil {
		t.Fatal(err)
	}
	cluster.Advance(idle)
	nothingLeft(t, cluster)
}

// A new allocation that fails to run, or whose claim fails otherwise than by another's claim, is
// cancelled, and the container fails.
func TestANewAllocationThatCannotBeClaimedIsCancelled(t *testing.T) {
	broken := &core.Failure{Kind: core.FailureInvalidInput, Message: "broken"}
	for name, op := range map[string]string{"await": "state", "claim": "claim"} {
		t.Run(name, func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			r := newRig(t, w, cluster, setup{idle: idle})
			f, err := r.env.Fingerprint(context.Background())
			if err != nil {
				t.Fatal(err)
			}
			cluster.SetFaults(func(o string) error {
				if o == op {
					return broken
				}
				return nil
			})
			_, err = startHeld(t, r, f.ImageDigest, "vibesys-cli-box")
			cluster.SetFaults(nil)
			if !errors.Is(err, broken) {
				t.Fatalf("the container: %v, want the failure of its %s", err, name)
			}
			if n := len(cluster.Submitted()); n != 1 {
				t.Errorf("%d allocations asked for, want 1", n)
			}
			nothingLeft(t, cluster)
		})
	}
}
