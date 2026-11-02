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
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// backstop bounds every wait. It only keeps a hung test from hanging the build: no test passes or
// fails by how long it took.
const backstop = time.Minute

var (
	commit = core.Commit("0123456789abcdef0123456789abcdef01234567")
	scope  = app.Scope{Repository: "/repo", Task: "task"}
	call   = core.CallContext{Repository: "/repo", Task: "task", Run: "run-1", Harness: "h", Agent: "agent-1"}
	// elsewhere and scopeElsewhere are a second task, for a job a test does not count.
	scopeElsewhere = app.Scope{Repository: "/repo", Task: "other"}
	elsewhere      = core.CallContext{Repository: "/repo", Task: "other", Run: "run-1", Harness: "h", Agent: "agent-1"}
)

// world is everything that outlives one daemon: the stores (the disk), the supervisors (their
// processes) and the machine's gates. newApp is a daemon started on it, and starting another
// after closing the first is a restart.
type world struct {
	t         *testing.T
	clock     *clock.Manual
	stores    *memstores.Stores
	gates     *fakeenv.MemoryGates
	env       *fakeenv.Environment
	checkouts *fakeenv.Checkouts
	survivors *supervisor.Survivors
	apps      []*app.App
}

func newWorld(t *testing.T) *world {
	t.Helper()
	gates := fakeenv.NewMemoryGates()
	w := &world{
		t:         t,
		clock:     clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond),
		stores:    memstores.New(app.Rules(5)),
		gates:     gates,
		env:       fakeenv.New(core.EnvironmentHost, gates),
		checkouts: &fakeenv.Checkouts{},
		survivors: supervisor.NewSurvivors(),
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
	})
	return w
}

func (w *world) supervisors() supervisor.Supervisors {
	return supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: w.env}, w.checkouts, w.survivors)
}

// newApp starts a daemon over the world. It fails the test if the daemon cannot start.
func (w *world) newApp() *app.App {
	w.t.Helper()
	a := app.New(instant(app.Config{
		Clock:        w.clock,
		Stores:       w.stores,
		Environments: fakeenv.Environments{core.EnvironmentHost: w.env},
		Supervisors:  w.supervisors(),
		Logf:         func(format string, args ...any) { w.t.Logf("daemon: "+format, args...) },
	}))
	w.apps = append(w.apps, a)
	if err := a.Start(context.Background()); err != nil {
		w.t.Fatalf("Start: %v", err)
	}
	return a
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

func command(script ...string) core.JobSpec {
	return core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Argv: script}}
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
