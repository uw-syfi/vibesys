package apptainer_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"strings"
	"sync"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
	"github.com/uw-syfi/vibesys/cli/internal/propcheck"
)

// siteMax is the longest time limit the fake site's QOS grants a job, as one site refuses anything
// over 12 hours.
const siteMax = 12 * time.Hour

// A measure of a task of two images, five samples beside the candidate's server with reuse on, runs
// on a site that refuses a job over its maximum: with the cap at that maximum, the one reused
// allocation asks for no more than it (and not for a day, the default), and every sample runs in
// it.
func TestAReusedMeasureOfATaskOfTwoImagesFitsTheSitesMaximum(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	cluster.SetMaxTime(siteMax)
	timers := envtest.NewTimers()
	r := newRig(t, w, cluster, setup{timers: timers, idle: idle, maxTime: siteMax, configure: func(c *docker.Config) {
		c.Candidates, c.Probe, c.ProbePause = candidateFiles{}, probeProgram, probePause
	}})
	for i := range 5 {
		a, out := serveAndRun(t, r, timers, []string{"ready", ";", "hold", "forever"}, []string{"print", fmt.Sprint("sample", i)})
		exited(t, a, 0)
		if !strings.Contains(out, fmt.Sprint("sample", i)) {
			t.Fatalf("sample %d: output %q", i, out)
		}
	}
	sub := cluster.Submitted()
	if len(sub) != 1 || sub[0].Limit != siteMax || !strings.HasPrefix(sub[0].Name, apptainer.PoolPrefix) {
		t.Errorf("allocations asked for %+v, want one reusable one of %v", sub, siteMax)
	}
}

// A container with no limit of its own does not make a reused allocation longer than the
// allocations' limit, which is what a site grants: it asks for that limit, claims the allocation
// with the most time left, and runs as long as it lasts.
func TestAContainerWithNoLimitDoesNotOutgrowAReusedAllocation(t *testing.T) {
	const allocation = 42000 * time.Second // under the site's maximum
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	cluster.SetMaxTime(siteMax)
	r := newRig(t, w, cluster, setup{idle: idle, allocationLimit: allocation})
	f, err := r.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	start := func(name string, limit time.Duration) docker.Container {
		t.Helper()
		c, err := r.engine.Start(ctx, docker.ContainerSpec{
			Name: name, Image: f.ImageDigest, Dir: "/work", Limit: limit, Measured: true, Labels: docker.Labels{docker.LabelScope: scope},
		})
		if err != nil {
			t.Fatalf("starting %s, with a limit of %v: %v", name, limit, err)
		}
		return c
	}
	remove := func(name string, c docker.Container) {
		t.Helper()
		c.Close()
		if err := r.engine.Remove(ctx, name); err != nil {
			t.Fatal(err)
		}
	}
	// Two allocations, the second asked for an hour later, so it has an hour more left.
	a := start("vibesys-cli-a", limit)
	cluster.Advance(time.Hour)
	b := start("vibesys-cli-b", limit)
	remove("vibesys-cli-a", a)
	remove("vibesys-cli-b", b)
	c := start("vibesys-cli-c", 0)
	sub := cluster.Submitted()
	if len(sub) != 2 || sub[0].Limit != allocation || sub[1].Limit != allocation {
		t.Fatalf("allocations asked for %+v, want two of %v", sub, allocation)
	}
	if claimed := cluster.Claimed(); len(claimed) != 1 || claimed[0] != sub[1].Name {
		t.Errorf("a container with no limit claimed %v, want the one with the most time left, %s", claimed, sub[1].Name)
	}
	remove("vibesys-cli-c", c)
	// With every allocation claimed, it asks for a new one of the allocations' limit.
	d, e := start("vibesys-cli-d", 0), start("vibesys-cli-e", 0)
	third := start("vibesys-cli-f", 0)
	if sub := cluster.Submitted(); len(sub) != 3 || sub[2].Limit != allocation {
		t.Errorf("allocations asked for %+v, want a third of %v", sub, allocation)
	}
	remove("vibesys-cli-d", d)
	remove("vibesys-cli-e", e)
	remove("vibesys-cli-f", third)
}

// A container whose limit and the margin that stops it do not fit under the cap is refused before
// anything is submitted, with or without reuse, saying which key caps it and by how much; one that
// fits exactly runs.
func TestAContainerLongerThanTheCapIsRefusedBeforeItIsSubmitted(t *testing.T) {
	for _, reuse := range []time.Duration{0, idle} {
		t.Run(fmt.Sprint("idle ", reuse), func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			cluster.SetMaxTime(siteMax)
			r := newRig(t, w, cluster, setup{idle: reuse, maxTime: siteMax})
			run := func(timeout time.Duration) core.Attempt {
				ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
				defer cancel()
				return r.env.Run(ctx, core.Execution{Checkout: checkout(t), Argv: []string{"exit", "0"}, Timeout: timeout, Measured: true}, &bytes.Buffer{})
			}
			f := failure(t, run(siteMax-apptainer.StopMargin+time.Minute))
			for _, want := range []string{"max_time_seconds = 43200", "11h51m0s", "12h1m0s"} {
				if f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, want) {
					t.Errorf("refusal %v, want an invalid input saying %q", f, want)
				}
			}
			if sub := cluster.Submitted(); len(sub) != 0 {
				t.Errorf("a refused container asked for %+v", sub)
			}
			exited(t, run(siteMax-apptainer.StopMargin), 0)
			if sub := cluster.Submitted(); len(sub) != 1 || sub[0].Limit != siteMax {
				t.Errorf("a container that fits asked for %+v, want one allocation of %v", sub, siteMax)
			}
		})
	}
}

// Whatever the cap, the allocations' limit under it, reuse, and the containers' limits (some with
// none) in any order: the cluster, which refuses a job over the cap, refuses nothing; a container
// is refused exactly when its limit and the stop's margin do not fit; and every allocation asked
// for a container is long enough for it.
func TestNoAllocationAsksForMoreThanTheCap(t *testing.T) {
	err := quick.Check(func(seed uint64) bool {
		rnd := rand.New(rand.NewPCG(seed, 7))
		maxTime := time.Duration(11+rnd.IntN(2000)) * time.Minute
		var allocation, reuse time.Duration
		if rnd.IntN(2) == 0 {
			reuse = idle
			if rnd.IntN(2) == 0 {
				allocation = time.Duration(1+rnd.Int64N(int64(maxTime/time.Second))) * time.Second
			}
		}
		w := fakeworld.New()
		cluster := clusterfake.New(w.MemoryGates)
		cluster.SetMaxTime(maxTime)
		r := newRig(t, w, cluster, setup{idle: reuse, allocationLimit: allocation, maxTime: maxTime})
		f, err := r.env.Fingerprint(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		ctx := context.Background()
		live := map[string]docker.Container{}
		for i := range 10 {
			name := fmt.Sprintf("vibesys-cli-box%d", rnd.IntN(3))
			if c, ok := live[name]; ok {
				c.Close()
				if err := r.engine.Remove(ctx, name); err != nil {
					t.Fatal(err)
				}
				delete(live, name)
				continue
			}
			var limit time.Duration
			if rnd.IntN(3) > 0 {
				limit = time.Duration(1+rnd.Int64N(int64(2*maxTime/time.Second))) * time.Second
			}
			cluster.Advance(time.Duration(rnd.Int64N(int64(maxTime))))
			before := len(cluster.Submitted())
			c, err := r.engine.Start(ctx, docker.ContainerSpec{
				Name: name, Image: f.ImageDigest, Dir: "/work", Limit: limit, Measured: rnd.IntN(2) == 0,
				Labels: docker.Labels{docker.LabelScope: scope},
			})
			fits := limit+apptainer.StopMargin <= maxTime || limit == 0
			var refused *core.Failure
			switch {
			case err != nil && (fits || !errors.As(err, &refused) || refused.Kind != core.FailureInvalidInput):
				t.Logf("step %d: cap %v, allocations %v: a container of limit %v was refused: %v", i, maxTime, allocation, limit, err)
				return false
			case err == nil && !fits:
				t.Logf("step %d: cap %v: a container of limit %v ran", i, maxTime, limit)
				return false
			case err != nil:
				if n := len(cluster.Submitted()); n != before {
					t.Logf("step %d: a refused container asked for an allocation", i)
					return false
				}
				continue
			}
			live[name] = c
			if sub := cluster.Submitted(); len(sub) > before {
				if got := sub[len(sub)-1].Limit; got > maxTime || (limit > 0 && got < limit+apptainer.StopMargin) {
					t.Logf("step %d: cap %v: a container of limit %v asked for %v", i, maxTime, limit, got)
					return false
				}
			}
		}
		for name, c := range live {
			c.Close()
			if err := r.engine.Remove(ctx, name); err != nil {
				t.Fatal(err)
			}
		}
		return true
	}, propcheck.Config(t, &quick.Config{MaxCount: 40}))
	if err != nil {
		t.Error(err)
	}
}

// An engine is refused a cap Slurm cannot ask for, one that leaves a container no time, and a
// reused allocation longer than the cap.
func TestAnEngineRefusesACapItCannotKeep(t *testing.T) {
	for name, c := range map[string]struct{ maxTime, allocation time.Duration }{
		"part of a minute":            {siteMax - time.Second, 0},
		"no time beyond the stop":     {apptainer.StopMargin, 0},
		"reused allocations too long": {siteMax, siteMax + time.Minute},
		"a negative cap":              {-time.Hour, 0},
	} {
		w := fakeworld.New()
		cluster := clusterfake.New(w.MemoryGates)
		local := newRig(t, w, cluster, setup{}).local
		_, err := apptainer.New(apptainer.Config{
			Local: local, Export: local, Cluster: cluster.Connect(), Root: root, Scope: scope,
			Timers: envtest.NewTimers(), Clock: clock.NewManual(time.Unix(1_700_000_000, 0), time.Second),
			Converter: clusterfake.NewConverter(), LocalDir: localDir,
			Idle: idle, AllocationLimit: c.allocation, MaxTime: c.maxTime,
		})
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Errorf("%s: %v, want an invalid input", name, err)
		}
	}
}

// serveAndRun runs command beside a server that runs serve, in the rig, firing the probes' pauses
// as they are armed.
func serveAndRun(t *testing.T, r *rig, timers *envtest.Timers, serve, command []string) (core.Attempt, string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	img, err := r.env.Candidate(ctx, core.CandidateBuild{Dockerfile: "serve/Dockerfile", Context: "serve"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	x := core.Execution{
		Checkout: checkout(t), Argv: command, Timeout: limit, Measured: true,
		Candidate: &core.CandidateRun{Image: img.ID, Serve: &core.CandidateServe{Argv: serve, Ready: "/health", StartupTimeout: startup}},
	}
	var (
		wg  sync.WaitGroup
		out bytes.Buffer
		a   core.Attempt
	)
	pumping, stop := context.WithCancel(ctx)
	armed := timers.Armed()
	wg.Go(func() {
		defer stop()
		a = r.env.Run(ctx, x, &out)
	})
	wg.Go(func() {
		for n := armed + 1; ; n++ {
			timer := timers.Await(pumping, n)
			if timer == nil {
				return
			}
			if timer.Duration == probePause {
				timer.Fire()
			}
		}
	})
	wg.Wait()
	return a, out.String()
}
