package app_test

import (
	"cmp"
	"context"
	"errors"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

const (
	repoA = "/repo-a"
	repoB = "/repo-b"
	// callLimit is how long a daemon of these tests gives a call. Only a call that hangs reaches it.
	callLimit = time.Hour
	// slowPeriod is the daemon's slow period, which passes when a test says.
	slowPeriod = time.Minute
)

var (
	callA  = core.CallContext{Scope: scopeAt(repoA, "task"), Run: "run-1", Harness: "h", Agent: "agent-1"}
	callB  = core.CallContext{Scope: scopeAt(repoB, "task"), Run: "run-1", Harness: "h", Agent: "agent-1"}
	scopeA = scopeAt(repoA, "task")
	scopeB = scopeAt(repoB, "task")

	errAway    = store.Mark(&core.Failure{Kind: core.FailureUnavailable, Message: "the repository cannot be read"}, store.ErrAway)
	errRemoved = store.Mark(&core.Failure{Kind: core.FailureUnavailable, Message: "the repository is not there"}, store.ErrAway, store.ErrGone)
)

// limits are timers for a daemon with a store that may hang. A retry takes no time. The slow
// period passes when the test steps it. The limit of a call falls only when that call hangs, so
// no call that answers is ever late.
type limits struct {
	mu    sync.Mutex
	armed map[context.Context]chan struct{}
	slow  []chan struct{}
}

func (l *limits) Timeout(ctx context.Context, d time.Duration) (<-chan struct{}, func()) {
	fired := make(chan struct{})
	if d < slowPeriod {
		close(fired)
		return fired, func() {}
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if d == slowPeriod {
		l.slow = append(l.slow, fired)
		return fired, func() {}
	}
	l.armed[ctx] = fired
	return fired, func() {
		l.mu.Lock()
		defer l.mu.Unlock()
		delete(l.armed, ctx)
	}
}

// awaitSlow waits until n things wait out the slow period.
func (l *limits) awaitSlow(t *testing.T, n int) {
	t.Helper()
	ctx := ctxFor(t)
	for {
		l.mu.Lock()
		got := len(l.slow)
		l.mu.Unlock()
		if got >= n {
			return
		}
		if ctx.Err() != nil {
			t.Fatalf("%d things wait out the slow period, want %d", got, n)
		}
		runtime.Gosched()
	}
}

// pass ends the slow period of everything waiting it out.
func (l *limits) pass() {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, c := range l.slow {
		close(c)
	}
	l.slow = nil
}

func (l *limits) expire(ctx context.Context) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if fired, ok := l.armed[ctx]; ok {
		close(fired)
		delete(l.armed, ctx)
	}
}

// outage is the stores of a machine whose repositories can go away, as a disk does, or hang, as a
// hard mount does. A hung call blocks until the test ends and ignores its context, as a call in
// the kernel does.
type outage struct {
	app.Stores
	w      *world
	limits *limits
	freed  chan struct{}

	// tries is how many times the daemon tries a call before it gives up; zero is attempts.
	tries int
	// stopTimeout is how long the daemon waits for a command it ended; zero is callLimit.
	stopTimeout time.Duration

	mu      sync.Mutex
	away    map[string]bool
	removed map[string]bool
	hung    map[string]bool
	refused map[string]int
	// missRunning, when set, is the next record of a job as running: it waits for release and
	// then fails as an away disk does, with nothing written.
	missRunning *gate
	// staleRead holds one refused read after it observed the outage.
	staleRead *gate
}

// gate holds one call until the test lets it go, and says when the call arrived.
type gate struct{ entered, release chan struct{} }

func newGate() *gate { return &gate{entered: make(chan struct{}), release: make(chan struct{})} }

// await waits until the call arrived.
func (g *gate) await(t *testing.T) {
	t.Helper()
	select {
	case <-g.entered:
	case <-ctxFor(t).Done():
		t.Fatal("the call never came")
	}
}

func newOutage(t *testing.T) (*world, *outage) {
	t.Helper()
	w := newWorld(t)
	o := &outage{
		Stores: w.stores, w: w, limits: &limits{armed: map[context.Context]chan struct{}{}}, freed: make(chan struct{}),
		away: map[string]bool{}, removed: map[string]bool{}, hung: map[string]bool{}, refused: map[string]int{},
	}
	t.Cleanup(func() { close(o.freed) })
	return w, o
}

// attempts is how many times the daemon over an outage tries a call before it gives up.
const attempts = 4

// build makes a daemon over the outage that has not started.
func (o *outage) build(sups supervisor.Supervisors) *app.App {
	a := app.New(app.Config{
		Clock: o.w.clock, Timers: o.limits, Stores: o, Supervisors: sups,
		Environments: fakeenv.Environments{core.EnvironmentHost: o.w.env},
		Retry:        app.Retry{Attempts: cmp.Or(o.tries, attempts), First: time.Millisecond, Longest: time.Millisecond},
		CallTimeout:  callLimit, StopTimeout: cmp.Or(o.stopTimeout, callLimit), SlowPeriod: slowPeriod, LogBatchDelay: time.Millisecond,
		Logf: func(string, ...any) {},
	})
	o.w.apps = append(o.w.apps, a)
	return a
}

// start starts a daemon over the outage.
func (o *outage) start(sups supervisor.Supervisors) *app.App {
	o.w.t.Helper()
	a := o.build(sups)
	if err := a.Start(ctxFor(o.w.t)); err != nil {
		o.w.t.Fatalf("Start: %v", err)
	}
	return a
}

func (o *outage) takeAway(repo string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.away[repo] = true
}

// remove deletes the repository: its directory is not there.
func (o *outage) remove(repo string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.removed[repo] = true
}

func (o *outage) takeMissRunning(t core.Transition) *gate {
	o.mu.Lock()
	defer o.mu.Unlock()
	g := o.missRunning
	if g == nil || t.To != core.JobRunning {
		return nil
	}
	o.missRunning = nil
	return g
}

func (o *outage) bringBack(repo string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	delete(o.away, repo)
}

// bringBackRemoved puts a removed repository back, as a new one would be.
func (o *outage) bringBackRemoved(repo string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	delete(o.removed, repo)
}

func (o *outage) hang(repo string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.hung[repo] = true
}

// reach is what a call to the repository meets.
func (o *outage) reach(ctx context.Context, repo string) error {
	o.mu.Lock()
	away, removed, hung := o.away[repo], o.removed[repo], o.hung[repo]
	if away || removed || hung {
		o.refused[repo]++
	}
	o.mu.Unlock()
	if removed {
		return errRemoved
	}
	if hung {
		o.limits.expire(ctx)
		<-o.freed
		return errAway
	}
	if away {
		o.mu.Lock()
		g := o.staleRead
		o.staleRead = nil
		o.mu.Unlock()
		if g != nil {
			close(g.entered)
			select {
			case <-g.release:
			case <-ctx.Done():
			}
		}
		return errAway
	}
	return nil
}

// awaitRefused waits until the repository has turned the daemon away n more times, which says the
// daemon met the outage and did not merely not get there yet. The slow period passes meanwhile.
func (o *outage) awaitRefused(t *testing.T, repo string, n int) {
	t.Helper()
	count := func() int {
		o.mu.Lock()
		defer o.mu.Unlock()
		return o.refused[repo]
	}
	target := count() + n
	ctx := ctxFor(t)
	for count() < target {
		if ctx.Err() != nil {
			t.Fatalf("%s turned the daemon away %d times, want %d", repo, count(), target)
		}
		o.limits.pass()
		runtime.Gosched()
	}
}

func (o *outage) Find(ctx context.Context, scope core.Scope) (store.Store, error) {
	if err := o.reach(ctx, string(scope.Repository())); err != nil {
		return nil, err
	}
	st, err := o.Stores.Find(ctx, scope)
	if err != nil {
		return nil, err
	}
	return outageStore{Store: st, o: o, repo: string(scope.Repository())}, nil
}

func (o *outage) Create(ctx context.Context, scope core.Scope) (store.Store, error) {
	if err := o.reach(ctx, string(scope.Repository())); err != nil {
		return nil, err
	}
	st, err := o.Stores.Create(ctx, scope)
	if err != nil {
		return nil, err
	}
	return outageStore{Store: st, o: o, repo: string(scope.Repository())}, nil
}

type outageStore struct {
	store.Store
	o    *outage
	repo string
}

func (s outageStore) Job(ctx context.Context, id core.JobID) (core.Job, error) {
	if err := s.o.reach(ctx, s.repo); err != nil {
		return core.Job{}, err
	}
	return s.Store.Job(ctx, id)
}

func (s outageStore) Jobs(ctx context.Context, f core.JobFilter) ([]core.Job, error) {
	if err := s.o.reach(ctx, s.repo); err != nil {
		return nil, err
	}
	return s.Store.Jobs(ctx, f)
}

func (s outageStore) Transition(ctx context.Context, t core.Transition) (core.Job, error) {
	if err := s.o.reach(ctx, s.repo); err != nil {
		return core.Job{}, err
	}
	if g := s.o.takeMissRunning(t); g != nil {
		close(g.entered)
		<-g.release
		return core.Job{}, errAway
	}
	return s.Store.Transition(ctx, t)
}

func (s outageStore) AppendLog(ctx context.Context, id core.JobID, data []byte) error {
	if err := s.o.reach(ctx, s.repo); err != nil {
		return err
	}
	return s.Store.AppendLog(ctx, id, data)
}

func (s outageStore) ReadLog(ctx context.Context, id core.JobID, off uint64, limit int) (core.LogChunk, error) {
	if err := s.o.reach(ctx, s.repo); err != nil {
		return core.LogChunk{}, err
	}
	return s.Store.ReadLog(ctx, id, off, limit)
}

func submitAs(t *testing.T, a *app.App, c core.CallContext, script ...string) core.Job {
	t.Helper()
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: c, Spec: command(script...)})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	return out.Job
}

// awaitIn waits until a job of the scope is as wanted, with the slow period passing meanwhile: the
// daemon gets there or the test's bound says it did not.
func (o *outage) awaitIn(t *testing.T, a *app.App, s core.Scope, id core.JobID, what string, ok func(core.Job) bool) core.Job {
	t.Helper()
	ctx := ctxFor(t)
	for {
		j, err := a.Job(ctx, s, id)
		if err == nil && ok(j) {
			return j
		}
		if ctx.Err() != nil {
			t.Fatalf("job %s of %s never was %s: last seen %s, %v", id, string(s.Repository()), what, j.State, err)
		}
		o.limits.pass()
		runtime.Gosched()
	}
}

// awaitFinal waits for a job to end with no slow period passing.
func (o *outage) awaitFinal(t *testing.T, a *app.App, s core.Scope, id core.JobID) {
	t.Helper()
	got, err := a.Job(ctxFor(t), s, id)
	if err != nil {
		t.Fatal(err)
	}
	if !got.State.Final() {
		err = a.Watch(ctxFor(t), s, []core.JobID{id}, func(core.Job) error { return nil })
	}
	if err != nil {
		t.Fatalf("Watch = %v", err)
	}
}

// awaitTurnedAway waits until the repository has turned the daemon away n times, with no slow
// period passing.
func (o *outage) awaitTurnedAway(t *testing.T, repo string, n int) {
	t.Helper()
	ctx := ctxFor(t)
	for {
		o.mu.Lock()
		got := o.refused[repo]
		o.mu.Unlock()
		if got >= n {
			return
		}
		if ctx.Err() != nil {
			t.Fatalf("%s turned the daemon away %d times, want %d", repo, got, n)
		}
		runtime.Gosched()
	}
}

func (o *outage) awaitFinalIn(t *testing.T, a *app.App, s core.Scope, id core.JobID) core.Job {
	t.Helper()
	return o.awaitIn(t, a, s, id, "final", func(j core.Job) bool { return j.State.Final() })
}

func refOf(s core.Scope, id core.JobID) lifecycle.Ref {
	return lifecycle.Ref{Store: lifecycle.StoreIDOf(string(s.Repository()), string(s.Task())), Job: id}
}

// P1-2: the output waits in the spool, so a store going away costs a running job nothing, however
// long it stays away.
func TestAStoreGoingAwayDoesNotStopARunningJob(t *testing.T) {
	w, o := newOutage(t)
	a := o.start(w.supervisors())
	job := submitAs(t, a, callA, "print", "A", ";", "hold", "g", ";", "print", "B", ";", "exit", "0")
	o.awaitIn(t, a, scopeA, job.ID, "running", func(j core.Job) bool { return j.State == core.JobRunning })
	o.takeAway(repoA)
	w.gates.Release("g")
	// More than the daemon's retries, each of which would have given the job up.
	o.awaitRefused(t, repoA, 20)
	o.bringBack(repoA)
	got := o.awaitFinalIn(t, a, scopeA, job.ID)
	if got.State != core.JobSucceeded {
		t.Fatalf("job = %s, %q; want it to succeed", got.State, got.CancelReason)
	}
}

// P1-2: a job whose store is away at its turn is not failed for it, and runs when the store is
// back.
func TestAStoreAwayAtAJobsTurnLeavesTheJobToRun(t *testing.T) {
	w, o := newOutage(t)
	a := o.start(w.supervisors())
	first := submitAs(t, a, callA, "hold", "g", ";", "exit", "0")
	o.awaitIn(t, a, scopeA, first.ID, "running", func(j core.Job) bool { return j.State == core.JobRunning })
	next := submitAs(t, a, callB, "print", "next", ";", "exit", "0")
	o.takeAway(repoB)
	w.gates.Release("g")
	o.awaitFinal(t, a, scopeA, first.ID)
	// No slow period passes, so what meets the outage is the launch and not a sweep. The launch tries
	// its store attempts times and then fails: bringing the store back any sooner would let a retry
	// of the launch through, and the job would never meet the outage at its turn.
	o.awaitTurnedAway(t, repoB, attempts)
	o.bringBack(repoB)
	got := o.awaitFinalIn(t, a, scopeB, next.ID)
	if got.State != core.JobSucceeded {
		t.Fatalf("job = %s; want it to run when its store is back", got.State)
	}
}

// P1-1: a supervisor that finished stays parked until it is released, which a store that is gone
// never lets happen. It runs nothing, so it holds up no other repository.
func TestAFinishedRunOfAGoneRepositoryHoldsUpNoOther(t *testing.T) {
	w, o := newOutage(t)
	first := o.start(w.supervisors())
	job := submitAs(t, first, callA, "hold", "g", ";", "exit", "0")
	o.awaitIn(t, first, scopeA, job.ID, "running", func(j core.Job) bool { return j.State == core.JobRunning })
	first.Close()
	o.takeAway(repoA)
	w.gates.Release("g")
	awaitIdle(t, w)

	second := o.start(w.supervisors())
	other := submitAs(t, second, callB, "print", "other", ";", "exit", "0")
	if got := o.awaitFinalIn(t, second, scopeB, other.ID); got.State != core.JobSucceeded {
		t.Fatalf("the other repository's job = %s", got.State)
	}
}

func awaitActive(t *testing.T, w *world) {
	t.Helper()
	ctx := ctxFor(t)
	for w.env.Active() == 0 {
		if ctx.Err() != nil {
			t.Fatal("the command never started")
		}
		runtime.Gosched()
	}
}

func awaitIdle(t *testing.T, w *world) {
	t.Helper()
	ctx := ctxFor(t)
	for w.env.Active() != 0 {
		if ctx.Err() != nil {
			t.Fatal("the command did not end")
		}
		runtime.Gosched()
	}
}

// P1-1: a live command of a repository that is gone may hold the machine, for as long as the job
// may run and no longer, and a waiter is told what it waits for.
func TestARunningJobOfAGoneRepositoryHoldsTheMachineUpToItsTimeout(t *testing.T) {
	w, o := newOutage(t)
	first := o.start(w.supervisors())
	spec := command("hold", "g", ";", "exit", "0")
	spec.Settings = core.CommandSettings{Image: core.ImageHarness, Argv: spec.Settings.(core.CommandSettings).Argv, Timeout: time.Minute}
	_, err := first.Submit(ctxFor(t), app.SubmitRequest{Context: callA, Spec: spec})
	if err != nil {
		t.Fatal(err)
	}
	// The command runs, and not only the store says so: a parked supervisor runs nothing and holds
	// nothing.
	awaitActive(t, w)
	first.Close()
	o.takeAway(repoA)

	second := o.start(w.supervisors())
	other := submitAs(t, second, callB, "print", "other", ";", "exit", "0")
	err = second.Watch(ctxFor(t), scopeB, []core.JobID{other.ID}, func(core.Job) error { return nil })
	var f *core.Failure
	if !errors.As(err, &f) || !strings.Contains(f.Message, "at the latest") {
		t.Fatalf("Watch of the job held up = %v, want why it waits and until when", err)
	}
	if n := w.ran("other"); n != 0 {
		t.Fatalf("the other job ran beside a live command within its bound: %d runs", n)
	}
	w.clock.Advance(2 * time.Minute)
	if got := o.awaitFinalIn(t, second, scopeB, other.ID); got.State != core.JobSucceeded {
		t.Fatalf("the other repository's job = %s", got.State)
	}
	w.gates.Release("g")
	awaitIdle(t, w)
}

// P2-1: a job whose supervisor died while its store could not say what the job was ends, and the
// calls that wait for it end with it.
// A job cancelled while its store did not take the record of its launch is shown queued, and the
// daemon moves it from there at once, even when it tries each call once: following the state the
// store shows does not spend a try that a later call would need.
func TestACancelDuringALaunchTheStoreMissedEndsOnOneTry(t *testing.T) {
	w, o := newOutage(t)
	o.tries = 1
	miss := newGate()
	o.missRunning = miss
	a := o.start(w.supervisors())
	job := submitAs(t, a, callA, "exit", "0")
	<-miss.entered
	type answer struct {
		job core.Job
		err error
	}
	done := make(chan answer, 1)
	go func() {
		got, err := a.Cancel(ctxFor(t), callA, job.ID, "enough")
		done <- answer{got, err}
	}()
	// The sweep and the cancel wait out the slow period: the cancel was asked of the machine
	// before the launch says how it went.
	o.limits.awaitSlow(t, 2)
	close(miss.release)
	ctx := ctxFor(t)
	for {
		select {
		case got := <-done:
			if got.err != nil || got.job.State != core.JobCancelled {
				t.Fatalf("Cancel = %s, %v; want cancelled", got.job.State, got.err)
			}
			return
		case <-ctx.Done():
			t.Fatal("Cancel did not answer")
		default:
		}
		o.limits.pass()
		runtime.Gosched()
	}
}

func TestAJobDroppedWhileItsStoreWasAwayEnds(t *testing.T) {
	for name, restart := range map[string]bool{"the daemon lives": false, "the daemon restarts": true} {
		t.Run(name, func(t *testing.T) {
			w, o := newOutage(t)
			a := o.start(w.supervisors())
			job := submitAs(t, a, callA, "mark", "started", ";", "hold", "g", ";", "exit", "0")
			// The store says running before the command is told to start, and a supervisor killed
			// before that runs the job again: the job is lost only once its command started.
			if !w.gates.Reached(ctxFor(t), "started") {
				t.Fatal("the command never started")
			}
			o.takeAway(repoA)
			if restart {
				a.Close()
				a = o.start(w.supervisors())
			}
			if !w.survivors.Kill(refOf(scopeA, job.ID)) {
				t.Fatal("no supervisor to kill")
			}
			o.awaitRefused(t, repoA, 20)
			o.bringBack(repoA)
			if got := o.awaitFinalIn(t, a, scopeA, job.ID); got.State != core.JobLost {
				t.Fatalf("job = %s, want lost", got.State)
			}
			got, err := a.Cancel(ctxFor(t), callA, job.ID, "enough")
			if err != nil || got.State != core.JobLost {
				t.Fatalf("Cancel = %s, %v", got.State, err)
			}
			if err := a.Watch(ctxFor(t), scopeA, []core.JobID{job.ID}, func(core.Job) error { return nil }); err != nil {
				t.Fatalf("Watch = %v", err)
			}
		})
	}
}

// P2-2: a repository on a hard mount that does not answer stops nothing but its own jobs, whether
// it hangs on a running daemon or a daemon starts with it hung.
func TestOneHungRepositoryLeavesTheRestServed(t *testing.T) {
	for name, restart := range map[string]bool{"while up": false, "at start": true} {
		t.Run(name, func(t *testing.T) {
			w, o := newOutage(t)
			a := o.start(w.supervisors())
			submitAs(t, a, callA, "exit", "0")
			if restart {
				a.Close()
			}
			o.hang(repoA)
			if restart {
				a = o.start(w.supervisors())
			}
			other := submitAs(t, a, callB, "print", "other", ";", "exit", "0")
			if got := o.awaitFinalIn(t, a, scopeB, other.ID); got.State != core.JobSucceeded {
				t.Fatalf("the other repository's job = %s", got.State)
			}
			if _, err := a.Job(ctxFor(t), scopeA, "j1"); !errors.Is(err, store.ErrAway) {
				t.Fatalf("a read of the hung repository = %v, want it to say so", err)
			}
		})
	}
}

// countedSupervisors counts the connections a daemon opens to supervisors and gives up.
type countedSupervisors struct {
	supervisor.Supervisors
	opened, closed atomic.Int64
}

func (s *countedSupervisors) count(run supervisor.Run) supervisor.Run {
	if alive, ok := run.(supervisor.Alive); ok {
		alive.Handle = s.handle(alive.Handle)
		return alive
	}
	return run
}

func (s *countedSupervisors) handle(h supervisor.Handle) supervisor.Handle {
	s.opened.Add(1)
	return countedHandle{Handle: h, s: s}
}

func (s *countedSupervisors) Spawn(ctx context.Context, l lifecycle.Launch) (supervisor.Handle, error) {
	h, err := s.Supervisors.Spawn(ctx, l)
	if err != nil {
		return nil, err
	}
	return s.handle(h), nil
}

func (s *countedSupervisors) Runs(ctx context.Context) ([]supervisor.Run, error) {
	runs, err := s.Supervisors.Runs(ctx)
	for i, run := range runs {
		runs[i] = s.count(run)
	}
	return runs, err
}

func (s *countedSupervisors) Probe(ctx context.Context, ref lifecycle.Ref) (supervisor.Run, error) {
	run, err := s.Supervisors.Probe(ctx, ref)
	if err != nil {
		return nil, err
	}
	return s.count(run), nil
}

type countedHandle struct {
	supervisor.Handle
	s *countedSupervisors
}

func (h countedHandle) Close() {
	h.s.closed.Add(1)
	h.Handle.Close()
}

// P2-3: every look at a supervisor opens a connection, and a store that stays away is looked at
// again and again. Every connection a daemon opened is given up by the time it is closed.
func TestEveryConnectionToASupervisorIsGivenUp(t *testing.T) {
	w, o := newOutage(t)
	sups := &countedSupervisors{Supervisors: w.supervisors()}
	a := o.start(sups)
	job := submitAs(t, a, callA, "hold", "g", ";", "exit", "0")
	o.awaitIn(t, a, scopeA, job.ID, "running", func(j core.Job) bool { return j.State == core.JobRunning })
	a.Close()
	o.takeAway(repoA)
	a = o.start(sups)
	o.awaitRefused(t, repoA, 20)
	o.bringBack(repoA)
	w.gates.Release("g")
	o.awaitFinalIn(t, a, scopeA, job.ID)
	a.Close()
	if opened, closed := sups.opened.Load(), sups.closed.Load(); opened != closed {
		t.Fatalf("%d connections opened and %d given up", opened, closed)
	}
}

// heldCancels is supervisors whose runs, as listed, take a cancel only when the test says: with a
// gate, a cancel waits for it, and with none, a cancel is dropped, as one that never reaches a
// command that ignores it.
type heldCancels struct {
	supervisor.Supervisors
	gate *gate
}

func (s heldCancels) Runs(ctx context.Context) ([]supervisor.Run, error) {
	runs, err := s.Supervisors.Runs(ctx)
	for i, run := range runs {
		if alive, ok := run.(supervisor.Alive); ok {
			alive.Handle = heldCancel{Handle: alive.Handle, gate: s.gate}
			runs[i] = alive
		}
	}
	return runs, err
}

type heldCancel struct {
	supervisor.Handle
	gate *gate
}

func (h heldCancel) Cancel(ctx context.Context, reason string) error {
	if h.gate == nil {
		return nil
	}
	close(h.gate.entered)
	select {
	case <-h.gate.release:
	case <-ctx.Done():
		return ctx.Err()
	}
	return h.Handle.Cancel(ctx, reason)
}

// lostStarts is supervisors whose start, as the daemon sends it when it launches a job, is lost: the
// store has the job running and its supervisor is still parked, which is what a daemon that went
// down between the two leaves behind. A run listed afterwards is the supervisor's own.
type lostStarts struct{ supervisor.Supervisors }

func (s lostStarts) Spawn(ctx context.Context, l lifecycle.Launch) (supervisor.Handle, error) {
	h, err := s.Supervisors.Spawn(ctx, l)
	if err != nil {
		return nil, err
	}
	return lostStart{Handle: h}, nil
}

type lostStart struct{ supervisor.Handle }

func (lostStart) Start(context.Context) error { return nil }

// #3834: a run whose command never started has no end to wait for until something starts it, so
// forgetting its repository ended only when the wait ran out, with the run reported left. It ends at
// once, with nothing run and nothing left, whatever the run was doing when the forget came.
func TestForgettingARepositoryEndsRunsInEveryPhase(t *testing.T) {
	for _, c := range []struct {
		name  string
		sups  func(*world) supervisor.Supervisors
		phase lifecycle.RunPhase
	}{
		{"parked", func(w *world) supervisor.Supervisors { return lostStarts{w.supervisors()} }, lifecycle.RunParked},
		{"running", func(w *world) supervisor.Supervisors { return w.supervisors() }, lifecycle.RunRunning},
	} {
		t.Run(c.name, func(t *testing.T) {
			w, o := newOutage(t)
			sups := c.sups(w)
			a := o.start(sups)
			job := submitAs(t, a, callA, "print", "ran", ";", "hold", "g", ";", "exit", "0")
			o.awaitIn(t, a, scopeA, job.ID, "running", func(j core.Job) bool { return j.State == core.JobRunning })
			if c.phase == lifecycle.RunRunning {
				// The store has the job running before its supervisor is told to start.
				awaitActive(t, w)
			}
			ref := lifecycle.Ref{Store: lifecycle.StoreIDOf(repoA, "task"), Job: job.ID}
			run, err := w.supervisors().Probe(ctxFor(t), ref)
			if err != nil {
				t.Fatal(err)
			}
			if alive, ok := run.(supervisor.Alive); !ok || alive.Phase != c.phase {
				t.Fatalf("before the forget the run is %+v, want alive and %s", run, c.phase)
			}
			o.remove(repoA)
			out, err := a.ForgetRepository(ctxFor(t), repoA)
			if err != nil {
				t.Fatal(err)
			}
			if len(out.Tasks) != 1 || len(out.Left) != 0 {
				t.Fatalf("forgotten = %+v, want its one task and nothing left", out)
			}
			requireNoRun(t, sups, ref)
			awaitIdle(t, w)
		})
	}
}

// #3762: forgetting a repository ends its commands before the machine lets its jobs go, so the next
// job does not start beside a command that is still ending.
func TestForgettingARepositoryEndsItsCommandsBeforeTheNextJobStarts(t *testing.T) {
	w, o := newOutage(t)
	cancel := newGate()
	a := o.start(heldCancels{Supervisors: w.supervisors(), gate: cancel})
	job := submitAs(t, a, callA, "hold", "g", ";", "exit", "0")
	o.awaitIn(t, a, scopeA, job.ID, "running", func(j core.Job) bool { return j.State == core.JobRunning })
	// The store records running before Start. Removing it while the supervisor is still
	// parked lets the machine safely admit the other job, since no command runs yet.
	awaitActive(t, w)
	other := submitAs(t, a, callB, "print", "other", ";", "exit", "0")
	o.remove(repoA)
	done := make(chan error, 1)
	go func() {
		_, err := a.ForgetRepository(ctxFor(t), repoA)
		done <- err
	}()
	cancel.await(t)
	if st := a.Status(); st.Queued != 1 {
		t.Fatalf("the next job left the queue while the forgotten repository's command still ran: %+v", st)
	}
	close(cancel.release)
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctxFor(t).Done():
		t.Fatal("the forget did not end")
	}
	if got := o.awaitFinalIn(t, a, scopeB, other.ID); got.State != core.JobSucceeded {
		t.Fatalf("the other repository's job = %s", got.State)
	}
}

// #3762: a command of a forgotten repository that does not end when it is told is reported left,
// and holds the machine while it can run, up to its limit, as it did before the forget.
func TestACommandLeftByAForgetHoldsTheMachineUpToItsLimit(t *testing.T) {
	w, o := newOutage(t)
	o.stopTimeout = time.Millisecond
	a := o.start(heldCancels{Supervisors: w.supervisors()})
	spec := command("hold", "g", ";", "exit", "0")
	spec.Settings = core.CommandSettings{Image: core.ImageHarness, Argv: spec.Settings.(core.CommandSettings).Argv, Timeout: time.Minute}
	if _, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: callA, Spec: spec}); err != nil {
		t.Fatal(err)
	}
	awaitActive(t, w)
	other := submitAs(t, a, callB, "print", "other", ";", "exit", "0")
	o.remove(repoA)
	out, err := a.ForgetRepository(ctxFor(t), repoA)
	if err != nil || len(out.Left) != 1 {
		t.Fatalf("ForgetRepository = %+v, %v; want the running job left", out, err)
	}
	if st := a.Status(); st.Queued != 1 {
		t.Fatalf("the next job left the queue beside a command left running within its limit: %+v", st)
	}
	o.limits.pass()
	if n := w.ran("other"); n != 0 {
		t.Fatalf("the other job ran beside a command left running within its limit: %d runs", n)
	}
	w.clock.Advance(2 * time.Minute)
	if got := o.awaitFinalIn(t, a, scopeB, other.ID); got.State != core.JobSucceeded {
		t.Fatalf("the other repository's job = %s", got.State)
	}
	w.gates.Release("g")
	awaitIdle(t, w)
}

// requireNoRun fails unless nothing is left of the job's run. A forget answers after this holds for
// the runs it forgot; a run of another repository that the daemon lets go of on its own is no part
// of that answer, so the test names the job it forgot.
func requireNoRun(t *testing.T, sups supervisor.Supervisors, ref lifecycle.Ref) {
	t.Helper()
	run, err := sups.Probe(ctxFor(t), ref)
	if err != nil {
		t.Fatalf("Probe(%s): %v", ref, err)
	}
	if _, none := run.(supervisor.None); !none {
		t.Fatalf("after the forget, the run of %s is %T, want none", ref, run)
	}
}

// A repository that is gone for good is given up: its jobs leave the machine, what is left of its
// runs is let go of, and its stores leave what the daemon knows. One that is there, one that cannot
// be reached, and one the daemon never heard of are refused, as forgetting cannot be undone.
func TestForgettingARepositoryThatIsGone(t *testing.T) {
	w, o := newOutage(t)
	sups := w.supervisors()
	a := o.start(sups)
	job := submitAs(t, a, callA, "hold", "g", ";", "exit", "0")
	o.awaitIn(t, a, scopeA, job.ID, "running", func(j core.Job) bool { return j.State == core.JobRunning })
	other := submitAs(t, a, callB, "print", "other", ";", "exit", "0")

	var f *core.Failure
	if _, err := a.ForgetRepository(ctxFor(t), repoA); !errors.As(err, &f) || f.Kind != core.FailureConflict {
		t.Fatalf("forgetting a repository that is there = %v, want a conflict", err)
	}
	if _, err := a.ForgetRepository(ctxFor(t), "/never"); !errors.As(err, &f) || f.Kind != core.FailureNotFound {
		t.Fatalf("forgetting a repository nobody knows = %v, want not found", err)
	}

	o.takeAway(repoA)
	a.Close()
	a = o.start(sups)
	// A store that cannot be read may be whole, with jobs that still run: only one whose place is
	// not there is gone.
	if _, err := a.ForgetRepository(ctxFor(t), repoA); !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Fatalf("forgetting a repository that cannot be reached = %v, want it refused as unavailable", err)
	}
	o.remove(repoA)
	out, err := a.ForgetRepository(ctxFor(t), repoA)
	if err != nil {
		t.Fatal(err)
	}
	if len(out.Tasks) != 1 || out.Tasks[0] != "task" || len(out.Left) != 0 {
		t.Fatalf("forgotten = %+v, want its one task and nothing left", out)
	}
	requireNoRun(t, sups, lifecycle.Ref{Store: lifecycle.StoreIDOf(repoA, "task"), Job: job.ID})
	awaitIdle(t, w)
	if got := o.awaitFinalIn(t, a, scopeB, other.ID); got.State != core.JobSucceeded {
		t.Fatalf("the other repository's job = %s", got.State)
	}
	known, err := w.stores.Known(ctxFor(t))
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range known {
		if string(s.Repository()) == repoA {
			t.Fatalf("the store of a forgotten repository is still known: %v", known)
		}
	}
}

func (s outageStore) Snapshot(ctx context.Context) (store.TaskSnapshot, error) {
	if err := s.o.reach(ctx, s.repo); err != nil {
		return store.TaskSnapshot{}, err
	}
	return s.Store.Snapshot(ctx)
}
