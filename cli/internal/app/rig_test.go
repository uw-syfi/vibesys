package app_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// backstop bounds every wait. It only keeps a hung test from hanging the build: no test passes or
// fails by how long it took.
const backstop = time.Minute

var (
	commit = core.Commit("0123456789abcdef0123456789abcdef01234567")
	scope  = scopeAt("/repo", "task")
	call   = core.CallContext{Scope: scopeAt("/repo", "task"), Run: "run-1", Harness: "h", Agent: "agent-1"}
	// elsewhere and scopeElsewhere are a second task, for a job a test does not count.
	scopeElsewhere = scopeAt("/repo", "other")
	elsewhere      = core.CallContext{Scope: scopeAt("/repo", "other"), Run: "run-1", Harness: "h", Agent: "agent-1"}
)

// world is everything that outlives one daemon: the stores (the disk), the supervisors (their
// processes) and the machine's gates. newApp is a daemon started on it, and starting another
// after closing the first is a restart.
type world struct {
	t         *testing.T
	clock     *clock.Manual
	stores    *memstores.Stores
	gates     *fakeenv.MemoryGates
	files     *hostfs.Memory
	tasks     *task.MemorySource
	env       *fakeenv.Environment
	checkouts *fakeenv.Checkouts
	survivors *supervisor.Survivors
	git       *gitrepo.Fake
	// branches is the repository's result branch, for a test of what the daemon holds a submission to.
	// Without it the daemon cannot read the branch.
	branches *gitrepo.FakeBranches
	// scheduler is how the daemons share the machine; the zero value runs one job at a time.
	scheduler app.Scheduler
	// leasing is how they share what they hold alone with other daemons; the zero value leases
	// nothing.
	leasing app.Leasing
	// allocations is how the daemons keep standing allocations; the zero value keeps none.
	allocations app.Allocations
	// timers and slowPeriod, when set, replace the instant timers and the slow period of an hour.
	timers     clock.Timers
	slowPeriod time.Duration
	apps       []*app.App
	// wrap, when set, wraps the supervisors each daemon starts jobs with, as a test that looks at
	// the launches does.
	wrap func(supervisor.Supervisors) supervisor.Supervisors
}

func newWorld(t *testing.T) *world {
	t.Helper()
	gates := fakeenv.NewMemoryGates()
	files := hostfs.NewMemory()
	clk := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond)
	machine := cpu.New(cpu.NewFakeSystem(rigMachine), clk)
	w := &world{
		t:         t,
		clock:     clk,
		stores:    memstores.New(app.Rules(5)),
		gates:     gates,
		files:     files,
		tasks:     task.NewMemorySource(),
		env:       fakeenv.New(core.EnvironmentHost, gates, fakeenv.WithFiles(files), fakeenv.WithMachine(machine)),
		checkouts: &fakeenv.Checkouts{},
		survivors: supervisor.NewSurvivors(),
		git:       gitrepo.NewFake(),
	}
	t.Cleanup(func() {
		for _, a := range w.apps {
			a.Close()
		}
		w.survivors.Close()
		if n := w.env.Active(); n != 0 {
			t.Errorf("%d commands still run after the test", n)
		}
		if n := w.checkouts.Open(); n != 0 {
			t.Errorf("%d checkouts still open after the test", n)
		}
		if left := w.files.Scratch(); len(left) != 0 {
			t.Errorf("scratch directories left: %v", left)
		}
	})
	return w
}

// rigMachine is what the world's environment reads of the machine its commands run on.
var rigMachine = core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", CPUModel: "Test CPU", LogicalCPUs: 8, MemoryBytes: 16 << 30}

func (w *world) supervisors() supervisor.Supervisors {
	host := supervisor.Host{Files: w.files, Clock: w.clock}
	var s supervisor.Supervisors = supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: w.env}, w.checkouts, w.survivors).WithHost(host)
	if w.wrap != nil {
		s = w.wrap(s)
	}
	return s
}

// newApp starts a daemon over the world. It fails the test if the daemon cannot start.
func (w *world) newApp() *app.App {
	w.t.Helper()
	a, err := w.tryNewApp()
	if err != nil {
		w.t.Fatalf("Start: %v", err)
	}
	return a
}

// tryNewApp is newApp that returns the error the daemon could not start with.
func (w *world) tryNewApp() (*app.App, error) {
	w.t.Helper()
	var branches gitrepo.Branches
	if w.branches != nil {
		branches = w.branches
	}
	cfg := instant(app.Config{
		Branches:     branches,
		Clock:        w.clock,
		Stores:       w.stores,
		Environments: fakeenv.Environments{core.EnvironmentHost: w.env},
		Supervisors:  w.supervisors(),
		Tasks:        w.taskSource(),
		Repositories: w.git,
		Scheduler:    w.scheduler,
		Leasing:      w.leasing,
		Allocations:  w.allocations,
		Logf:         func(format string, args ...any) { w.t.Logf("daemon: "+format, args...) },
	})
	if w.timers != nil {
		cfg.Timers = w.timers
	}
	if w.slowPeriod > 0 {
		cfg.SlowPeriod = w.slowPeriod
	}
	a := app.New(cfg)
	w.apps = append(w.apps, a)
	return a, a.Start(context.Background())
}

// instant makes every wait of a retry end at once, so a test sees the retries and not their pace,
// and leaves the long waits that only guard against a hang to never fire.
func instant(cfg app.Config) app.Config {
	cfg.Timers = clock.Immediate{Longest: time.Second}
	cfg.Retry = app.Retry{Attempts: 4, First: time.Millisecond, Longest: time.Millisecond}
	cfg.LogBatchDelay = time.Millisecond
	cfg.CallTimeout, cfg.StopTimeout, cfg.SlowPeriod = time.Hour, time.Hour, time.Hour
	return cfg
}

func ctxFor(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), backstop)
	t.Cleanup(cancel)
	return ctx
}

// noPlan is what a test whose subject is not plans says about the plan of a job it submits. The
// tests call as a run, and a measure from a run must say what it tests, so each of these tests says
// none; a check or a command job takes no such statement.
func noPlan(spec core.JobSpec) bool { return spec.Settings.Kind() == core.JobMeasure }

func command(script ...string) core.JobSpec {
	return core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Image: core.ImageHarness, Argv: script}}
}

func submit(t *testing.T, a *app.App, script ...string) core.Job {
	t.Helper()
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: command(script...)})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if out.Disposition != core.SubmitCreated {
		t.Fatalf("Submit disposition = %s, want created (a command job is never joined)", out.Disposition)
	}
	return out.Job
}

var errDone = errors.New("done")

// await waits until the job satisfies the predicate. It waits on the daemon's own change signal,
// so it returns when the state is reached and not after a time.
func await(t *testing.T, a *app.App, id core.JobID, what string, ok func(core.Job) bool) core.Job {
	t.Helper()
	var got core.Job
	err := a.Watch(ctxFor(t), scope, []core.JobID{id}, func(j core.Job) error {
		got = j
		if ok(j) {
			return errDone
		}
		return nil
	})
	if err != nil && !errors.Is(err, errDone) {
		t.Fatalf("waiting for job %s to be %s: %v (last seen %s)", id, what, err, got.State)
	}
	if !ok(got) {
		t.Fatalf("job %s ended as %s, never %s", id, got.State, what)
	}
	return got
}

func awaitState(t *testing.T, a *app.App, id core.JobID, want core.JobState) core.Job {
	return await(t, a, id, string(want), func(j core.Job) bool { return j.State == want })
}

func awaitFinal(t *testing.T, a *app.App, id core.JobID) core.Job {
	return await(t, a, id, "final", func(j core.Job) bool { return j.State.Final() })
}

// logOf returns the job's whole stored output.
func logOf(t *testing.T, a *app.App, id core.JobID) string {
	t.Helper()
	var buf bytes.Buffer
	err := a.ReadLogs(ctxFor(t), scope, id, 0, false, func(c core.LogChunk) error {
		buf.Write(c.Data)
		return nil
	})
	if err != nil {
		t.Fatalf("ReadLogs: %v", err)
	}
	return buf.String()
}

// awaitOutput waits until the job's stored output contains the text.
func awaitOutput(t *testing.T, a *app.App, id core.JobID, text string) {
	t.Helper()
	var buf bytes.Buffer
	err := a.ReadLogs(ctxFor(t), scope, id, 0, true, func(c core.LogChunk) error {
		buf.Write(c.Data)
		if bytes.Contains(buf.Bytes(), []byte(text)) {
			return errDone
		}
		return nil
	})
	if !errors.Is(err, errDone) {
		t.Fatalf("waiting for %q in the output of %s: %v; output %q", text, id, err, buf.String())
	}
}

// ran returns how many times a command that starts with the first word of the script ran.
func (w *world) ran(first string) int {
	n := 0
	for _, x := range w.env.Runs() {
		if len(x.Argv) > 1 && x.Argv[1] == first {
			n++
		}
	}
	return n
}

func describe(j core.Job) string {
	return fmt.Sprintf("%s %s", j.ID, j.State)
}

// taskSource is the world's tasks, or no source at all when the test took them away.
func (w *world) taskSource() task.Source {
	if w.tasks == nil {
		return nil
	}
	return w.tasks
}

// scopeAt is the scope of a task in a repository, as the daemon resolves one. Tests name
// repositories by path, so the path is the repository.
func scopeAt(repo, task string) core.Scope {
	s, err := core.ResolvedScope(core.Repository(repo), core.TaskName(task))
	if err != nil {
		panic(err)
	}
	return s
}
