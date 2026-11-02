package app

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/changed"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// App is the daemon's job flows. Create it with New, call Start once, and Close it to stop.
type App struct {
	cfg       Config
	ctx       context.Context
	stop      context.CancelFunc
	wg        sync.WaitGroup
	startedAt time.Time

	// admit is held while a job is made known to the machine, and while a cancel finds out
	// whether the machine knows of the job. A job is therefore never in a store, queued, without
	// the machine having been told, and a cancel never reaches the machine before the job does.
	admit sync.Mutex

	// branchMu is held while a result branch is read for a decision or moved, so promotions go
	// one at a time and nothing reads the branch between a recorded promotion and its move.
	branchMu sync.Mutex

	jobsChanged changed.Signal
	logsChanged changed.Signal

	mu        sync.Mutex
	state     lifecycle.State
	tasks     map[lifecycle.StoreID]*taskEntry
	handles   map[lifecycle.Ref]supervisor.Handle
	discarded map[lifecycle.Ref]bool
	closed    bool
}

// taskEntry is a store the daemon has opened.
type taskEntry struct {
	scope Scope
	store store.Store
}

// New returns an App that is recovering: it launches nothing until Start has told it what the
// stores and the supervisors hold.
func New(cfg Config) *App {
	cfg = cfg.withDefaults()
	ctx, stop := context.WithCancel(context.Background())
	return &App{
		cfg: cfg, ctx: ctx, stop: stop, startedAt: cfg.Clock.Now(),
		state:     lifecycle.Recovering(),
		tasks:     map[lifecycle.StoreID]*taskEntry{},
		handles:   map[lifecycle.Ref]supervisor.Handle{},
		discarded: map[lifecycle.Ref]bool{},
	}
}

// Close stops the App: what it was waiting for is abandoned, and it returns once everything it
// started has returned. Supervisors are not touched: they outlive the daemon, and the next start
// finds them.
func (a *App) Close() {
	a.mu.Lock()
	a.closed = true
	a.mu.Unlock()
	a.stop()
	a.wg.Wait()
}

// dispatch tells the machine of an event and carries out what it asks for.
func (a *App) dispatch(e lifecycle.Event) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closed {
		return
	}
	var reqs []lifecycle.Request
	a.state, reqs = lifecycle.Step(a.state, e)
	for _, r := range reqs {
		a.wg.Add(1)
		go func() {
			defer a.wg.Done()
			a.execute(r)
		}()
	}
}

func (a *App) now() time.Time { return a.cfg.Clock.Now() }

// notifyJobs wakes whoever waits on a change of a job's state or log.
func (a *App) notifyJobs() {
	a.jobsChanged.Notify()
	a.logsChanged.Notify()
}

// open returns the store of the scope, opening it once.
func (a *App) open(ctx context.Context, scope Scope) (*taskEntry, error) {
	if err := scope.Validate(); err != nil {
		return nil, err
	}
	id := lifecycle.StoreIDOf(scope.Repository, scope.Task)
	a.mu.Lock()
	t, ok := a.tasks[id]
	a.mu.Unlock()
	if ok {
		return t, nil
	}
	st, err := a.cfg.Stores.Open(ctx, scope)
	if err != nil {
		return nil, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if t, ok := a.tasks[id]; ok {
		return t, nil
	}
	t = &taskEntry{scope: scope, store: st}
	a.tasks[id] = t
	return t, nil
}

func (a *App) taskOf(id lifecycle.StoreID) (*taskEntry, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if t, ok := a.tasks[id]; ok {
		return t, nil
	}
	return nil, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("the daemon holds no store %s", id)}
}

func (a *App) handleOf(ref lifecycle.Ref) supervisor.Handle {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.handles[ref]
}

func (a *App) setHandle(ref lifecycle.Ref, h supervisor.Handle) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.handles[ref] = h
}

func (a *App) dropHandle(ref lifecycle.Ref) {
	a.mu.Lock()
	defer a.mu.Unlock()
	delete(a.handles, ref)
}

// failureOf is the failure an error stands for.
func failureOf(err error) core.Failure {
	var f *core.Failure
	if errors.As(err, &f) {
		return *f
	}
	return core.Failure{Kind: core.FailureInternal, Message: err.Error()}
}

// Status is what the daemon reports about its work.
type Status struct {
	StartedAt time.Time
	Queued    int
	Running   int
}

// Status returns the machine's counts.
func (a *App) Status() Status {
	a.mu.Lock()
	defer a.mu.Unlock()
	queued, running := a.state.Counts()
	return Status{StartedAt: a.startedAt, Queued: queued, Running: running}
}
