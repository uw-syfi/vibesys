package sim_test

import (
	"context"
	"os"
	"testing"
	"testing/synctest"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
	"github.com/uw-syfi/vibesys/cli/internal/production"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// These tests make the daemon's production wiring (production.Process.Wire) over fakes of what lies
// beyond the process (Process.Outside), and run the real App over it. RunDaemon itself needs
// supervisor processes of its own.

// standingConfig is the operator config a user adds an allocations section to, to hold standing
// allocations of an apptainer placement: the placement's own sections, and the allocations one.
const standingConfig = `
[placements.cluster-a]
kind = "apptainer"
[placements.cluster-a.remote]
host = "host-a"
remote_workspace_root = "/shared/vibesys"
[placements.cluster-a.slurm]
partition = "part-a"
max_time_seconds = 7200
[placements.cluster-a.allocations]
max_held = 3
max_for = "2h"
fit_margin = "1m"
# The rig's runs make no call for hours at a time between their steps; a run is ended for its
# silence only where a test says so.
run_idle_end = "1000h"
`

const clusterPlacement = "cluster-a"

var (
	standingCommit = core.Commit("0123456789abcdef0123456789abcdef01234567")
	standingScope  = mustScope("/repo", "task")
	standingCall   = core.CallContext{Scope: standingScope, Run: "run-1", Harness: "h", Agent: "agent-1"}
)

func mustScope(repo, name string) core.Scope {
	s, err := core.ResolvedScope(core.Repository(repo), core.TaskName(name))
	if err != nil {
		panic(err)
	}
	return s
}

// observeTimers fires the allocation clock only when a test ticks it, and ends every other wait
// of up to Longest at once, as clock.Immediate does. The period of the allocation clock is one no
// other wait of the daemon asks for.
type observeTimers struct {
	clock.Immediate
	ticks chan chan struct{}
}

func newObserveTimers() *observeTimers {
	return &observeTimers{Immediate: clock.Immediate{Longest: time.Minute}, ticks: make(chan chan struct{}, 1)}
}

// Timeout implements clock.Timers.
func (t *observeTimers) Timeout(ctx context.Context, d time.Duration) (<-chan struct{}, func()) {
	if d != app.DefaultObservePeriod {
		return t.Immediate.Timeout(ctx, d)
	}
	fired := make(chan struct{})
	t.ticks <- fired
	return fired, func() {}
}

// standingRig is a daemon of the production wiring over a cluster that outlives it: the log and the
// identity of its allocations are on disk, the stores are its disk, and the cluster is Slurm.
type standingRig struct {
	t       *testing.T
	cluster *clusterfake.Cluster
	gates   *fakeenv.MemoryGates
	clock   *clock.Manual
	timers  *observeTimers
	stores  *memstores.Stores
	repos   *gitrepo.Fake
	tasks   *task.MemorySource
	// survivors and checkouts are the supervisors' processes and the checkouts they hold, which a
	// restart of the daemon leaves as they are.
	survivors *supervisor.Survivors
	checkouts *fakeenv.Checkouts
	indexes   string
	// runtime names the runtime directory of the rig's daemon, which a restart keeps, as a daemon's
	// restart on one machine does: what it keeps for itself under the state home is keyed by it. Each
	// start opens a directory of its own, so that what an earlier daemon's survivors hold there is
	// not in the way of the next.
	runtime string
	process production.Process
	op      *cliv1.OperatorConfig
}

// inStandingBubble runs a test of a standing rig in a synctest bubble: the daemon's goroutines are
// the bubble's, so the test can wait until they are all blocked (settle) instead of for a while, and
// a daemon that never gets there ends the test at once with a deadlock instead of hanging it.
func inStandingBubble(t *testing.T, f func(t *testing.T)) {
	t.Helper()
	synctest.Test(t, f)
}

func newStandingRig(t *testing.T, config string) *standingRig {
	t.Helper()
	w := fakeworld.New()
	r := &standingRig{
		t: t, cluster: clusterfake.New(w.MemoryGates), gates: w.MemoryGates,
		// The clock holds still on a read, so the time a step takes does not depend on how often the
		// daemon's loops read it on this machine. Time moves only by passes.
		clock: clock.NewManual(clusterfake.Epoch, 0), timers: newObserveTimers(),
		stores: memstores.New(app.Rules(5)), repos: gitrepo.NewFake(), tasks: task.NewMemorySource(), survivors: supervisor.NewSurvivors(), checkouts: &fakeenv.Checkouts{}, indexes: t.TempDir(), runtime: "/run/user/1000/vibesys-cli", op: parseConfig(t, config),
	}
	r.repos.AddRepo("/repo", nil)
	t.Cleanup(r.survivors.Close)
	r.tasks.Put("/repo", standingCommit, tasktest.Scripted("task", []string{"exit", "0"}, []string{"exit", "0"}))
	local := dockerfake.NewDaemon(w.MemoryGates).Connect()
	r.process = production.Process{UID: 1000, GID: 1000, Home: "/home/u", Getenv: func(string) string { return "" }, Outside: production.Outside{
		Cluster:   func(string) apptainer.Cluster { return r.cluster.Connect() },
		Docker:    local,
		Converter: clusterfake.NewConverter(), Files: r.tasks, Probe: "/probe/vibesys-cli",
		Source:  dockerfake.Fixed(docker.Spec{Dockerfile: []byte("FROM busybox:1.36.1\n")}),
		Machine: docker.Shared{As: docker.User{UID: 1000, GID: 1000}},
	}}
	return r
}

// clocks is the rig's time: the manual clock of the daemon, and the timers of its waits.
type rigClocks struct {
	*clock.Manual
	clock.Timers
}

// rigMachine is the machine the rig's daemon runs on.
const rigMachine = "host-a"

// start is a daemon of the wiring, started on what the earlier ones left.
func (r *standingRig) start() (*app.App, *daemon.Server) {
	r.t.Helper()
	return r.startAs(rigMachine, r.runtime)
}

// startAs is a daemon of the wiring on the machine named, whose runtime directory is named runtime,
// over the rig's state home and cluster. Its clock is the rig's from then on: tick lets it go.
func (r *standingRig) startAs(machine, runtime string) (*app.App, *daemon.Server) {
	r.t.Helper()
	// The directory is made on the real disk, so the real user owns it, whatever uid the simulated process has.
	dir, err := runtimedir.Open(shortDir(r.t), os.Getuid(), runtimedir.System())
	if err != nil {
		r.t.Fatal(err)
	}
	// Each daemon waits on a clock of its own, so that a tick is never let go to one that is gone.
	r.timers = newObserveTimers()
	clk := rigClocks{r.clock, r.timers}
	wired, err := r.process.Wire(r.op, clk, dir, production.AllocationsDir(r.indexes, machine, runtime))
	if err != nil {
		r.t.Fatal(err)
	}
	a := app.New(app.Config{
		Clock: r.clock, Timers: r.timers, Stores: r.stores, Environments: wired.Environments,
		Supervisors:  supervisor.NewInProcess(wired.Environments, r.checkouts, r.survivors).WithHost(supervisor.Host{Files: hostfs.NewMemory(), Clock: r.clock}).WithProcessesLeft(wired.ProcessesLeft),
		Tasks:        r.tasks,
		Repositories: r.repos, Scheduler: wired.Scheduler, Placements: wired.Placements, Allocations: wired.Allocations,
		Retry:       app.Retry{Attempts: 4, First: time.Millisecond, Longest: time.Millisecond},
		CallTimeout: time.Hour, StopTimeout: time.Hour, SlowPeriod: time.Hour, LogBatchDelay: time.Millisecond,
		Logf: func(format string, args ...any) { r.t.Logf("daemon: "+format, args...) },
	})
	if err := a.Start(context.Background()); err != nil {
		r.t.Fatalf("Start: %v", err)
	}
	r.t.Cleanup(a.Close)
	srv := daemon.NewServer(a, "test", daemon.Inherited{}).WithCapacities(wired.Capacities)
	r.t.Cleanup(srv.Shutdown)
	return a, srv
}

// ctx is a call's context, bounded only so that a hang ends the test.
func (r *standingRig) ctx() context.Context {
	r.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	r.t.Cleanup(cancel)
	return ctx
}

// tick is one turn of the daemon's allocation loop: it lets the allocation clock fire once, and
// returns once the daemon has done all that look started, when every goroutine of the bubble is
// blocked on something only the test can give it (synctest.Wait). So what the test reads after a
// tick is the daemon's state after exactly that many turns, on any machine.
func (r *standingRig) tick() {
	r.t.Helper()
	select {
	case fired := <-r.timers.ticks:
		close(fired)
	case <-r.ctx().Done():
		r.t.Fatal("the daemon never waited on its allocation clock")
	}
	synctest.Wait()
}

// passes moves the time of the daemon and of the cluster together.
func (r *standingRig) passes(d time.Duration) {
	r.clock.Advance(d)
	r.cluster.Advance(d)
}

// await returns once the allocation is in the state, ticking the allocation clock as the daemon
// waits for it: the daemon looks at its backend once per tick.
func (r *standingRig) await(a *app.App, id alloc.ID, until app.AllocUntil) app.AllocationView {
	r.t.Helper()
	for {
		v, outcome, err := a.WaitAllocation(r.ctx(), id, until, time.Millisecond)
		if err != nil {
			r.t.Fatalf("waiting for %s: %v", id, err)
		}
		if outcome == app.AllocWaitMet {
			return v
		}
		if outcome == app.AllocWaitEnded {
			r.t.Fatalf("allocation %s ended before it was %s: %+v", id, until.Kind, v.Phase)
		}
		r.tick()
	}
}

func (r *standingRig) jobIn(a *app.App, into string, argv ...string) core.Job {
	r.t.Helper()
	out, err := a.Submit(r.ctx(), app.SubmitRequest{Context: standingCall, Spec: core.JobSpec{
		Commit: standingCommit, Environment: core.EnvironmentDocker, Placement: clusterPlacement, Allocation: into,
		Settings: core.CommandSettings{Image: core.ImageHarness, Argv: argv, Timeout: 5 * time.Minute},
	}})
	if err != nil {
		r.t.Fatalf("Submit into %q: %v", into, err)
	}
	return out.Job
}

func parseConfig(t *testing.T, text string) *cliv1.OperatorConfig {
	t.Helper()
	c, err := operator.Parse([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	return c
}

// shortDir is a directory of a path short enough for a socket's, which the runtime directory holds.
func shortDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "s")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
}
