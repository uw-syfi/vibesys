package app_test

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

var errCrashed = errors.New("the daemon crashed")

// crash makes the daemon die at a chosen step. A step is a call that changes something outside
// the daemon: a write to a store, or a call that starts, cancels or releases a supervisor. The
// step numbered limit+1 and every later one fails without taking effect, and so does every read,
// which is what a dead process does. A limit below zero never crashes.
type crash struct {
	limit int64
	steps atomic.Int64
	dead  atomic.Bool
	// onDeath, set before the first step, runs once when the daemon dies. It stands for the
	// connection of a client breaking: a client that waits on a dead daemon is told.
	onDeath func()
	once    sync.Once
}

func (c *crash) step() error {
	n := c.steps.Add(1)
	if c.limit >= 0 && n > c.limit {
		c.dead.Store(true)
		c.once.Do(func() {
			if c.onDeath != nil {
				c.onDeath()
			}
		})
		return errCrashed
	}
	return nil
}

func (c *crash) read() error {
	if c.dead.Load() {
		return errCrashed
	}
	return nil
}

type crashStores struct {
	inner app.Stores
	c     *crash
}

func (s crashStores) Open(ctx context.Context, scope app.Scope) (store.Store, error) {
	if err := s.c.read(); err != nil {
		return nil, err
	}
	st, err := s.inner.Open(ctx, scope)
	if err != nil {
		return nil, err
	}
	return crashStore{Store: st, c: s.c}, nil
}

func (s crashStores) Known(ctx context.Context) ([]app.Scope, error) { return s.inner.Known(ctx) }

type crashStore struct {
	store.Store
	c *crash
}

func (s crashStore) Submit(ctx context.Context, sub core.Submission) (core.SubmitOutcome, error) {
	if err := s.c.step(); err != nil {
		return core.SubmitOutcome{}, err
	}
	return s.Store.Submit(ctx, sub)
}

func (s crashStore) Transition(ctx context.Context, t core.Transition) (core.Job, error) {
	if err := s.c.step(); err != nil {
		return core.Job{}, err
	}
	return s.Store.Transition(ctx, t)
}

func (s crashStore) AddNote(ctx context.Context, id core.JobID, n core.Note) (core.Job, error) {
	if err := s.c.step(); err != nil {
		return core.Job{}, err
	}
	return s.Store.AddNote(ctx, id, n)
}

func (s crashStore) AppendLog(ctx context.Context, id core.JobID, data []byte) error {
	if err := s.c.step(); err != nil {
		return err
	}
	return s.Store.AppendLog(ctx, id, data)
}

func (s crashStore) Job(ctx context.Context, id core.JobID) (core.Job, error) {
	if err := s.c.read(); err != nil {
		return core.Job{}, err
	}
	return s.Store.Job(ctx, id)
}

func (s crashStore) Jobs(ctx context.Context, f core.JobFilter) ([]core.Job, error) {
	if err := s.c.read(); err != nil {
		return nil, err
	}
	return s.Store.Jobs(ctx, f)
}

func (s crashStore) ReadLog(ctx context.Context, id core.JobID, off uint64, max int) (core.LogChunk, error) {
	if err := s.c.read(); err != nil {
		return core.LogChunk{}, err
	}
	return s.Store.ReadLog(ctx, id, off, max)
}

type crashSupervisors struct {
	supervisor.Supervisors
	c *crash
}

func (s crashSupervisors) Spawn(ctx context.Context, l lifecycle.Launch) (supervisor.Handle, error) {
	if err := s.c.step(); err != nil {
		return nil, err
	}
	h, err := s.Supervisors.Spawn(ctx, l)
	if err != nil {
		return nil, err
	}
	return crashHandle{Handle: h, c: s.c}, nil
}

func (s crashSupervisors) Runs(ctx context.Context) ([]supervisor.Run, error) {
	if err := s.c.read(); err != nil {
		return nil, err
	}
	runs, err := s.Supervisors.Runs(ctx)
	for i := range runs {
		if runs[i].Handle != nil {
			runs[i].Handle = crashHandle{Handle: runs[i].Handle, c: s.c}
		}
	}
	return runs, err
}

func (s crashSupervisors) Forget(ctx context.Context, ref lifecycle.Ref) error {
	if err := s.c.step(); err != nil {
		return err
	}
	return s.Supervisors.Forget(ctx, ref)
}

type crashHandle struct {
	supervisor.Handle
	c *crash
}

func (h crashHandle) Start(ctx context.Context) error {
	if err := h.c.step(); err != nil {
		return err
	}
	return h.Handle.Start(ctx)
}

func (h crashHandle) Cancel(ctx context.Context, reason string) error {
	if err := h.c.step(); err != nil {
		return err
	}
	return h.Handle.Cancel(ctx, reason)
}

func (h crashHandle) Release(ctx context.Context) error {
	if err := h.c.step(); err != nil {
		return err
	}
	return h.Handle.Release(ctx)
}

func (h crashHandle) Follow(ctx context.Context, off uint64, emit func([]byte) error) (lifecycle.Outcome, error) {
	if err := h.c.read(); err != nil {
		return lifecycle.Outcome{}, err
	}
	return h.Handle.Follow(ctx, off, func(data []byte) error {
		if err := h.c.read(); err != nil {
			return err
		}
		return emit(data)
	})
}

// startApp starts a daemon over stores and supervisors that the world keeps.
func (w *world) startApp(stores app.Stores, sups supervisor.Supervisors) *app.App {
	w.t.Helper()
	a := app.New(app.Config{
		Clock: w.clock, Stores: stores, Supervisors: sups,
		Environments: fakeenv.Environments{core.EnvironmentHost: w.env},
		Logf:         func(format string, args ...any) { w.t.Logf("daemon: "+format, args...) },
	})
	w.apps = append(w.apps, a)
	if err := a.Start(ctxFor(w.t)); err != nil {
		w.t.Fatalf("Start: %v", err)
	}
	return a
}

// scenario runs two jobs through a daemon that crashes at the given step, restarts it, and
// returns the number of steps a run without a crash takes. With reboot, the restart is a
// machine restart: every supervisor is killed too.
func scenario(t *testing.T, limit int64, reboot bool) int64 {
	w := newWorld(t)
	watching, hangUp := context.WithCancel(ctxFor(t))
	defer hangUp()
	c := &crash{limit: limit, onDeath: hangUp}
	a1 := w.startApp(crashStores{inner: w.stores, c: c}, crashSupervisors{Supervisors: w.supervisors(), c: c})

	var ids []core.JobID
	for _, script := range [][]string{
		{"print", "A", ";", "hold", "g", ";", "print", "A2", ";", "exit", "0"},
		{"print", "B", ";", "exit", "0"},
	} {
		out, err := a1.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: command(script...)})
		if err != nil {
			if !c.dead.Load() {
				t.Fatalf("Submit: %v", err)
			}
			break
		}
		ids = append(ids, out.Job.ID)
	}
	// Open the gate when the first job is seen running, and wait for the jobs to end, or for the
	// daemon to die.
	if len(ids) > 0 {
		_ = a1.Watch(watching, scope, ids, func(j core.Job) error {
			if j.ID == ids[0] && j.State == core.JobRunning {
				w.gates.Release("g")
			}
			return nil
		})
	}
	total := c.steps.Load()
	if c.dead.Load() {
		a1.Close()
		if reboot {
			w.survivors.Reboot()
		}
		a2 := w.newApp()
		w.gates.Release("g")
		all, err := a2.Jobs(ctxFor(t), scope, core.JobFilter{})
		if err != nil {
			t.Fatal(err)
		}
		for _, j := range all {
			awaitFinal(t, a2, j.ID)
		}
		check(t, w, a2, reboot, limit)
	} else {
		check(t, w, a1, reboot, limit)
	}
	return total
}

// check says what must hold of the world after a crash and a restart, wherever the crash was.
func check(t *testing.T, w *world, a *app.App, reboot bool, limit int64) {
	t.Helper()
	jobs, err := a.Jobs(ctxFor(t), scope, core.JobFilter{})
	if err != nil {
		t.Fatal(err)
	}
	where := fmt.Sprintf("crash at step %d, reboot %v", limit, reboot)
	want := map[string]string{"A": "A\nA2\n", "B": "B\n"}
	for _, j := range jobs {
		if !j.State.Final() {
			t.Errorf("%s: job %s is %s, never final", where, j.ID, j.State)
			continue
		}
		var name string
		if strings.Contains(strings.Join(argvOf(j), " "), "hold") {
			name = "A"
		} else {
			name = "B"
		}
		// A command runs at most once, whatever happens to the daemon, and a lost job is not
		// retried.
		if n := w.ran(name); n > 1 {
			t.Errorf("%s: command %s ran %d times", where, name, n)
		}
		out := logOf(t, a, j.ID)
		switch j.State {
		case core.JobSucceeded:
			if out != want[name] {
				t.Errorf("%s: job %s output = %q, want exactly %q", where, name, out, want[name])
			}
			if w.ran(name) != 1 {
				t.Errorf("%s: job %s succeeded but its command ran %d times", where, name, w.ran(name))
			}
		case core.JobLost:
			if !reboot {
				t.Errorf("%s: job %s is lost although no supervisor was killed", where, name)
			}
			if !strings.HasPrefix(want[name], strings.SplitN(out, "vibesys-cli", 2)[0]) {
				t.Errorf("%s: lost job %s has output %q that is not a prefix of %q", where, name, out, want[name])
			}
		default:
			t.Errorf("%s: job %s ended %s", where, name, j.State)
		}
	}
	if !reboot && len(jobs) == 2 {
		for _, j := range jobs {
			if j.State != core.JobSucceeded {
				t.Errorf("%s: job %s is %s; with no supervisor killed both jobs succeed", where, j.ID, j.State)
			}
		}
	}
	if w.survivors != nil {
		w.survivors.Close()
	}
	if w.env.Active() != 0 || w.checkouts.Open() != 0 {
		t.Errorf("%s: %d commands and %d checkouts left", where, w.env.Active(), w.checkouts.Open())
	}
}

func argvOf(j core.Job) []string {
	if c, ok := j.Spec.Settings.(core.CommandSettings); ok {
		return c.Argv
	}
	return nil
}

// A daemon that crashes at any step of two jobs' lives, however the steps fall, comes back to a
// state where each job ended once and none ran twice.
func TestADaemonCrashAtAnyStepRecovers(t *testing.T) {
	total := scenario(t, -1, false)
	if total < 10 {
		t.Fatalf("the scenario took %d steps; it is too small to mean anything", total)
	}
	for limit := int64(0); limit <= total+2; limit++ {
		t.Run(fmt.Sprintf("step %d", limit), func(t *testing.T) { scenario(t, limit, false) })
	}
}

// The same with the machine restarting: the supervisors die too, so a job running at the crash
// is lost, and nothing else changes.
func TestAMachineRestartAtAnyStepRecovers(t *testing.T) {
	total := scenario(t, -1, true)
	for limit := int64(0); limit <= total+2; limit++ {
		t.Run(fmt.Sprintf("step %d", limit), func(t *testing.T) { scenario(t, limit, true) })
	}
}
