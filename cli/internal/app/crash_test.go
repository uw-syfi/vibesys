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
	if c.dead.Load() || (c.limit >= 0 && n > c.limit) {
		c.kill()
		return errCrashed
	}
	return nil
}

// kill makes the daemon dead now, whatever step it is at.
func (c *crash) kill() {
	c.dead.Store(true)
	c.once.Do(func() {
		if c.onDeath != nil {
			c.onDeath()
		}
	})
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

func (s crashStores) Find(ctx context.Context, scope core.Scope) (store.Store, error) {
	return s.open(scope, func() (store.Store, error) { return s.inner.Find(ctx, scope) })
}

func (s crashStores) Create(ctx context.Context, scope core.Scope) (store.Store, error) {
	return s.open(scope, func() (store.Store, error) { return s.inner.Create(ctx, scope) })
}

func (s crashStores) open(_ core.Scope, get func() (store.Store, error)) (store.Store, error) {
	if err := s.c.read(); err != nil {
		return nil, err
	}
	st, err := get()
	if err != nil {
		return nil, err
	}
	return crashStore{Store: st, c: s.c}, nil
}

func (s crashStores) Known(ctx context.Context) ([]core.Scope, error) { return s.inner.Known(ctx) }

func (s crashStores) Forget(ctx context.Context, scope core.Scope) error {
	return s.inner.Forget(ctx, scope)
}

func (s crashStores) GivenUp(ctx context.Context, scope core.Scope) ([]core.JobID, error) {
	if err := s.c.read(); err != nil {
		return nil, err
	}
	return s.inner.GivenUp(ctx, scope)
}

func (s crashStores) Reclaim(ctx context.Context, scope core.Scope, job core.JobID) error {
	if err := s.c.step(); err != nil {
		return err
	}
	return s.inner.Reclaim(ctx, scope, job)
}

func (s crashStores) Close() error { return s.inner.Close() }

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
	for i, run := range runs {
		if alive, ok := run.(supervisor.Alive); ok {
			alive.Handle = crashHandle{Handle: alive.Handle, c: s.c}
			runs[i] = alive
		}
	}
	return runs, err
}

func (s crashSupervisors) Forget(ctx context.Context, ref lifecycle.Ref, proof lifecycle.DeathProof) error {
	if err := s.c.step(); err != nil {
		return err
	}
	return s.Supervisors.Forget(ctx, ref, proof)
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
	a := app.New(instant(app.Config{
		Clock: w.clock, Stores: stores, Supervisors: sups,
		Environments: fakeenv.Environments{core.EnvironmentHost: w.env},
		Logf:         func(format string, args ...any) { w.t.Logf("daemon: "+format, args...) },
	}))
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
	if limit < 0 {
		// The last steps come after the last job is final. A next job starts only after them, so
		// counting then counts them all. It is in its own task, which check does not look at.
		next, err := a1.Submit(ctxFor(t), app.SubmitRequest{Context: elsewhere, Spec: command("exit", "0")})
		if err != nil {
			t.Fatal(err)
		}
		if err := a1.Watch(ctxFor(t), scopeElsewhere, []core.JobID{next.Job.ID}, func(core.Job) error { return nil }); err != nil {
			t.Fatal(err)
		}
		check(t, w, a1, reboot, limit)
		return c.steps.Load()
	}
	// Steps can remain after the jobs are final, and one of them may be the crash, so the daemon
	// dies here if it has not, and only the restarted one is read.
	c.kill()
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
	return c.steps.Load()
}

// leavingStores refuses opens as a store is refused while the daemon before is still dying: the
// kernel frees the machine's lock first and the store's a moment later.
type leavingStores struct {
	app.Stores
	mu     sync.Mutex
	left   int
	opened int
}

func (s *leavingStores) Find(ctx context.Context, scope core.Scope) (store.Store, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.opened++
	if s.left > 0 {
		s.left--
		return nil, errors.Join(&core.Failure{Kind: core.FailureConflict, Message: "in use"}, store.ErrOwnerLeaving)
	}
	return s.Stores.Find(ctx, scope)
}

func (s *leavingStores) attempts() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.opened
}

func (w *world) newAppOver(stores app.Stores) *app.App {
	w.t.Helper()
	a := app.New(instant(app.Config{
		Clock: w.clock, Stores: stores, Environments: fakeenv.Environments{core.EnvironmentHost: w.env},
		Supervisors: w.supervisors(),
		Logf:        func(format string, args ...any) { w.t.Logf("daemon: "+format, args...) },
	}))
	w.apps = append(w.apps, a)
	if err := a.Start(context.Background()); err != nil {
		w.t.Fatalf("Start: %v", err)
	}
	return a
}

// A daemon started the moment the last one died answers for that one's jobs: it does not put the
// store aside for the slow sweep because the store is not free yet.
func TestARestartWaitsForADaemonThatIsStillLeaving(t *testing.T) {
	w := newWorld(t)
	a1 := w.newApp()
	job := submit(t, a1, "hold", "g")
	awaitState(t, a1, job.ID, core.JobRunning)
	a1.Close()

	stores := &leavingStores{Stores: w.stores, left: 3}
	a2 := w.newAppOver(stores)
	w.gates.Release("g")
	if got := awaitFinal(t, a2, job.ID); got.State != core.JobSucceeded {
		t.Fatalf("job = %s, want succeeded", got.State)
	}
}

// A store that never comes free costs a bounded wait, and is then away like any other.
func TestARestartStopsWaitingForAStoreThatStaysHeld(t *testing.T) {
	w := newWorld(t)
	a1 := w.newApp()
	job := submit(t, a1, "hold", "g")
	awaitState(t, a1, job.ID, core.JobRunning)
	a1.Close()

	stores := &leavingStores{Stores: w.stores, left: 1 << 30}
	w.newAppOver(stores)
	if got := stores.attempts(); got != 4 {
		t.Fatalf("%d attempts at the store, want the 4 of the retry policy", got)
	}
	w.gates.Release("g")
}

// cancelScenario cancels a running job through a daemon that crashes at the given step, restarts
// it, and returns the number of steps a run without a crash takes. A cancel that was answered
// holds through the crash: the gate stays shut, so the job can only end by being cancelled.
func cancelScenario(t *testing.T, limit int64) int64 {
	w := newWorld(t)
	watching, hangUp := context.WithCancel(ctxFor(t))
	defer hangUp()
	c := &crash{limit: limit, onDeath: hangUp}
	a1 := w.startApp(crashStores{inner: w.stores, c: c}, crashSupervisors{Supervisors: w.supervisors(), c: c})

	answered := false
	out, err := a1.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: command("print", "A", ";", "hold", "g", ";", "print", "A2", ";", "exit", "0")})
	switch {
	case err == nil:
		_ = a1.Watch(watching, scope, []core.JobID{out.Job.ID}, func(j core.Job) error {
			if j.State != core.JobRunning || answered {
				return nil
			}
			// Tied to the crash: a dead daemon's cancel answers nobody, and no retry of it comes.
			if _, err := a1.Cancel(watching, call, j.ID, "enough"); err == nil {
				answered = true
			} else if !c.dead.Load() {
				t.Fatalf("Cancel: %v", err)
			}
			return nil
		})
	case !c.dead.Load():
		t.Fatalf("Submit: %v", err)
	}
	if limit < 0 {
		next, err := a1.Submit(ctxFor(t), app.SubmitRequest{Context: elsewhere, Spec: command("exit", "0")})
		if err != nil {
			t.Fatal(err)
		}
		if err := a1.Watch(ctxFor(t), scopeElsewhere, []core.JobID{next.Job.ID}, func(core.Job) error { return nil }); err != nil {
			t.Fatal(err)
		}
		return c.steps.Load()
	}
	c.kill()
	a1.Close()
	a2 := w.newApp()
	if !answered {
		w.gates.Release("g")
	}
	jobs, err := a2.Jobs(ctxFor(t), scope, core.JobFilter{})
	if err != nil {
		t.Fatal(err)
	}
	where := fmt.Sprintf("crash at step %d", limit)
	for _, j := range jobs {
		final := awaitFinal(t, a2, j.ID)
		if answered && final.State != core.JobCancelled {
			t.Errorf("%s: an answered cancel ended as %s", where, final.State)
		}
		if !answered && final.State != core.JobCancelled && final.State != core.JobSucceeded {
			t.Errorf("%s: the job ended as %s", where, final.State)
		}
		if n := w.ran("A"); n > 1 {
			t.Errorf("%s: the command ran %d times", where, n)
		}
	}
	return c.steps.Load()
}

func TestACancelHoldsThroughACrashAtAnyStep(t *testing.T) {
	total := cancelScenario(t, -1)
	if total < 10 {
		t.Fatalf("the scenario took %d steps; it is too small to mean anything", total)
	}
	for limit := int64(0); limit <= total+2; limit++ {
		t.Run(fmt.Sprintf("step %d", limit), func(t *testing.T) { cancelScenario(t, limit) })
	}
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
		// A command runs at most once, whatever happens to the daemon: a command job runs again
		// only when its attempt was cut off before its command started, and a lost job is not run
		// again.
		if n := w.ran(name); n > 1 {
			t.Errorf("%s: command %s ran %d times", where, name, n)
		}
		out, cuts := withoutCutLines(logOf(t, a, j.ID))
		if cuts != len(j.Attempts) || (cuts > 0 && !reboot) {
			t.Errorf("%s: job %s has %d attempts cut off and says %d, with reboot %v", where, name, len(j.Attempts), cuts, reboot)
		}
		for _, past := range j.Attempts {
			if _, before := past.Cut.(core.CutBeforeStart); !before {
				t.Errorf("%s: command job %s was queued again after its command may have started: %+v", where, name, past)
			}
		}
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

// withoutCutLines returns a job's output less the lines that say an attempt was cut off and the
// job runs again, and how many there were.
func withoutCutLines(out string) (string, int) {
	var kept strings.Builder
	cuts := 0
	for _, line := range strings.SplitAfter(out, "\n") {
		if strings.HasPrefix(line, "vibesys-cli: attempt ") && strings.Contains(line, "running it again") {
			cuts++
			continue
		}
		kept.WriteString(line)
	}
	return kept.String(), cuts
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

// requeueScenario has a daemon die at step first, the machine restart, a second daemon die at
// step second, and a daemon on another machine end the job, given up, or cancel it. It returns the job as the third left it.
func requeueScenario(t *testing.T, first, second int64) (core.Job, string) {
	w := newWorld(t)
	watching1, hangUp1 := context.WithCancel(ctxFor(t))
	defer hangUp1()
	c1 := &crash{limit: first, onDeath: hangUp1}
	a1 := w.startApp(crashStores{inner: w.stores, c: c1}, crashSupervisors{Supervisors: w.supervisors(), c: c1})
	out, err := a1.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: command("print", "A", ";", "mark", "started", ";", "hold", "g", ";", "exit", "0")})
	if err != nil {
		if !c1.dead.Load() {
			t.Fatalf("Submit: %v", err)
		}
		return core.Job{}, ""
	}
	id := out.Job.ID
	// The first daemon goes on until it dies or the command starts, which the gate then holds.
	w.gates.Reached(watching1, "started")
	c1.kill()
	a1.Close()
	w.survivors.Reboot()

	watching, hangUp := context.WithCancel(ctxFor(t))
	defer hangUp()
	before, err := w.stores.Find(ctxFor(t), scope)
	if err != nil {
		t.Fatal(err)
	}
	was, err := before.Job(ctxFor(t), id)
	if err != nil {
		t.Fatal(err)
	}
	c2 := &crash{limit: second, onDeath: hangUp}
	a2 := w.startApp(crashStores{inner: w.stores, c: c2}, crashSupervisors{Supervisors: w.supervisors(), c: c2})
	// The second daemon goes on until it dies or moves the job.
	_ = a2.Watch(watching, scope, []core.JobID{id}, func(j core.Job) error {
		if j.State != was.State || len(j.Attempts) != len(was.Attempts) {
			return errDone
		}
		return nil
	})
	c2.kill()
	a2.Close()

	// The machine is gone for good, and the user gives the job up: a daemon elsewhere, which finds no
	// run of it, ends it.
	elsewhere := supervisor.NewSurvivors()
	t.Cleanup(elsewhere.Close)
	envs := fakeenv.Environments{core.EnvironmentHost: w.env}
	a3 := w.startApp(givenUp{Stores: w.stores, job: id}, supervisor.NewInProcess(envs, w.checkouts, elsewhere))
	if _, err := a3.Cancel(ctxFor(t), call, id, "enough"); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	final := awaitFinal(t, a3, id)
	return final, logOf(t, a3, id)
}

// givenUp is Stores in which the user gave one job up.
type givenUp struct {
	app.Stores
	job core.JobID
}

func (s givenUp) GivenUp(ctx context.Context, scope core.Scope) ([]core.JobID, error) {
	given, err := s.Stores.GivenUp(ctx, scope)
	return append(given, s.job), err
}

// A job's output says it runs again only for an attempt its store records as cut off: a daemon that
// dies while it queues a job again, and a job that ends before it runs again, leave no line that
// promises a run that never comes, and no recorded attempt without its line.
func TestALineSayingAJobRunsAgainMatchesItsRecordedAttempts(t *testing.T) {
	for first := int64(0); first <= 8; first++ {
		for second := int64(0); second <= 10; second++ {
			job, out := requeueScenario(t, first, second)
			if job.ID == "" {
				continue
			}
			if _, cuts := withoutCutLines(out); cuts != len(job.Attempts) {
				t.Errorf("crash at steps %d and %d: the job ended %s with %d attempts recorded and output %q", first, second, job.State, len(job.Attempts), out)
			}
		}
	}
}

func (s crashStore) Snapshot(ctx context.Context) (store.TaskSnapshot, error) {
	if err := s.c.read(); err != nil {
		return store.TaskSnapshot{}, err
	}
	return s.Store.Snapshot(ctx)
}
