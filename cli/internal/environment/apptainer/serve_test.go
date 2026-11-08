package apptainer_test

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

// serveOnCluster starts a job-scoped server that runs serve on a cluster of idle allocations
// when idle is set, firing the probes' pauses as they are armed.
func serveOnCluster(t *testing.T, idle time.Duration, serve ...string) (environment.Service, *clusterfake.Cluster, fakeworld.World) {
	t.Helper()
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	timers := envtest.NewTimers()
	r := newRig(t, w, cluster, setup{timers: timers, idle: idle, configure: func(c *docker.Config) {
		c.Probe, c.ProbePause = probeProgram, probePause
	}})
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	pumping, stop := context.WithCancel(ctx)
	var wg sync.WaitGroup
	wg.Go(func() {
		for n := 1; ; n++ {
			timer := timers.Await(pumping, n)
			if timer == nil {
				return
			}
			if timer.Duration == probePause {
				timer.Fire()
			}
		}
	})
	js := core.JobService{Checkout: checkout(t), Argv: serve, Ready: "/health", StartupTimeout: startup, Lifetime: 3 * time.Hour}
	sv, a := r.env.Serve(ctx, js, nil)
	stop()
	wg.Wait()
	if a != nil {
		t.Fatalf("start %+v", a)
	}
	return sv, cluster, w
}

// On a cluster the server has the allocation, asked for once for as long as the job may use it,
// and each run's command is a step of that allocation of its own, which ends with the run; the
// allocation, or the claim of a reused one, ends with Close.
func TestAJobServerHoldsOneAllocationThatEveryRunJoins(t *testing.T) {
	for _, idle := range []time.Duration{0, time.Hour} {
		sv, cluster, _ := serveOnCluster(t, idle, "print", "serving", ";", "ready", ";", "hold", "forever")
		sub := cluster.Submitted()
		if len(sub) != 1 || sub[0].Limit < 3*time.Hour {
			t.Fatalf("idle %v: allocations %+v, want one for the job's lifetime", idle, sub)
		}
		for range 2 {
			ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
			a := sv.Run(ctx, core.Execution{Checkout: checkout(t), Argv: []string{"print", "sampled"}, Timeout: limit, Measured: true}, nil)
			cancel()
			exited(t, a, 0)
			if !sv.Alive() {
				t.Fatalf("idle %v: the server ended with a run", idle)
			}
		}
		if n := len(cluster.Submitted()); n != 1 {
			t.Fatalf("idle %v: %d allocations after two runs, want the server's alone", idle, n)
		}
		names := map[string]bool{}
		for _, b := range cluster.Started() {
			names[b.Name] = true
		}
		services := 0
		for name := range names {
			if strings.HasSuffix(name, "-service") {
				services++
			}
		}
		if services != 1 || len(names) != 3 {
			t.Fatalf("idle %v: boxes %v, want the server's and one for each run", idle, names)
		}
		if err := sv.Close(); err != nil {
			t.Fatal(err)
		}
		if running := cluster.Running(); len(running) != 0 {
			t.Fatalf("idle %v: processes left: %v", idle, running)
		}
		if idle == 0 {
			nothingLeft(t, cluster)
		}
	}
}
