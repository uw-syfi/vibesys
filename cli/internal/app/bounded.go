package app

import (
	"context"
	"errors"
	"fmt"
	"path"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/owned"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// boundedStores makes every call to a store end in a bounded time. A store on a hard mount that
// does not answer blocks in the kernel, where no context reaches it, so the caller goes on without
// the call and the call is left to finish when the disk answers. A store runs one request at a
// time, so what is tried next runs after the call that was left, and sees what it did.
type boundedStores struct {
	inner  Stores
	timers clock.Timers
	limit  time.Duration
}

// errNoAnswer says a store did not answer in time, and it is marked as away, which says nothing of
// the jobs in it.
var errNoAnswer = store.Mark(&core.Failure{Kind: core.FailureUnavailable, Message: "the store did not answer in time"}, store.ErrAway)

// bounded runs do with a time limit, and leaves it behind at the limit.
func bounded[T any](ctx context.Context, timers clock.Timers, limit time.Duration, do func(context.Context) (T, error)) (T, error) {
	var zero T
	bound, done := clock.Deadline(ctx, timers, limit)
	defer done()
	type answer struct {
		value T
		err   error
	}
	answered := make(chan answer, 1)
	owned.Detached(func() {
		v, err := do(bound)
		answered <- answer{v, err}
	})
	select {
	case a := <-answered:
		// A call the limit ended answers with the limit's error, which the limit says better.
		if a.err == nil || bound.Err() == nil {
			return a.value, a.err
		}
	case <-bound.Done():
	}
	if err := ctx.Err(); err != nil {
		return zero, err
	}
	return zero, errNoAnswer
}

func (s boundedStores) store(st store.Store, err error) (store.Store, error) {
	if err != nil {
		return nil, err
	}
	return boundedStore{inner: st, timers: s.timers, limit: s.limit}, nil
}

func (s boundedStores) Find(ctx context.Context, scope Scope) (store.Store, error) {
	return s.store(bounded(ctx, s.timers, s.limit, func(ctx context.Context) (store.Store, error) { return s.inner.Find(ctx, scope) }))
}

func (s boundedStores) Create(ctx context.Context, scope Scope) (store.Store, error) {
	return s.store(bounded(ctx, s.timers, s.limit, func(ctx context.Context) (store.Store, error) { return s.inner.Create(ctx, scope) }))
}

func (s boundedStores) Known(ctx context.Context) ([]Scope, error) {
	return bounded(ctx, s.timers, s.limit, s.inner.Known)
}

func (s boundedStores) Forget(ctx context.Context, scope Scope) error {
	_, err := bounded(ctx, s.timers, s.limit, func(ctx context.Context) (struct{}, error) { return struct{}{}, s.inner.Forget(ctx, scope) })
	return err
}

// Close is not bounded, as giving a store up only drops a lock.
func (s boundedStores) Close() error { return s.inner.Close() }

// boundedStore is one store with every call bounded.
type boundedStore struct {
	inner  store.Store
	timers clock.Timers
	limit  time.Duration
}

var _ store.Store = boundedStore{}

func call[T any](s boundedStore, ctx context.Context, do func(store.Store, context.Context) (T, error)) (T, error) {
	return bounded(ctx, s.timers, s.limit, func(ctx context.Context) (T, error) { return do(s.inner, ctx) })
}

func (s boundedStore) Submit(ctx context.Context, sub core.Submission) (core.SubmitOutcome, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) (core.SubmitOutcome, error) { return st.Submit(ctx, sub) })
}

func (s boundedStore) Job(ctx context.Context, id core.JobID) (core.Job, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) (core.Job, error) { return st.Job(ctx, id) })
}

func (s boundedStore) Jobs(ctx context.Context, filter core.JobFilter) ([]core.Job, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) ([]core.Job, error) { return st.Jobs(ctx, filter) })
}

func (s boundedStore) Transition(ctx context.Context, t core.Transition) (core.Job, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) (core.Job, error) { return st.Transition(ctx, t) })
}

func (s boundedStore) AddNote(ctx context.Context, id core.JobID, note core.Note) (core.Job, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) (core.Job, error) { return st.AddNote(ctx, id, note) })
}

func (s boundedStore) Results(ctx context.Context, filter core.ResultFilter) ([]core.Result, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) ([]core.Result, error) { return st.Results(ctx, filter) })
}

func (s boundedStore) RecordPromotion(ctx context.Context, p core.Approved) error {
	_, err := call(s, ctx, func(st store.Store, ctx context.Context) (struct{}, error) {
		return struct{}{}, st.RecordPromotion(ctx, p)
	})
	return err
}

func (s boundedStore) Promotions(ctx context.Context) ([]core.Promotion, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) ([]core.Promotion, error) { return st.Promotions(ctx) })
}

func (s boundedStore) CreatePlan(ctx context.Context, plan core.NewPlan) (core.Plan, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) (core.Plan, error) { return st.CreatePlan(ctx, plan) })
}

func (s boundedStore) Plan(ctx context.Context, number core.PlanNumber) (core.Plan, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) (core.Plan, error) { return st.Plan(ctx, number) })
}

func (s boundedStore) Plans(ctx context.Context, filter core.PlanFilter) ([]core.Plan, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) ([]core.Plan, error) { return st.Plans(ctx, filter) })
}

func (s boundedStore) EditPlan(ctx context.Context, number core.PlanNumber, edit core.PlanEdit) (core.Plan, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) (core.Plan, error) { return st.EditPlan(ctx, number, edit) })
}

func (s boundedStore) AppendEvent(ctx context.Context, draft core.EventDraft) (core.Event, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) (core.Event, error) { return st.AppendEvent(ctx, draft) })
}

func (s boundedStore) Events(ctx context.Context, run core.RunID, after uint64, limit int) ([]core.Event, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) ([]core.Event, error) {
		return st.Events(ctx, run, after, limit)
	})
}

func (s boundedStore) AppendLog(ctx context.Context, id core.JobID, data []byte) error {
	_, err := call(s, ctx, func(st store.Store, ctx context.Context) (struct{}, error) {
		return struct{}{}, st.AppendLog(ctx, id, data)
	})
	return err
}

func (s boundedStore) ReadLog(ctx context.Context, id core.JobID, offset uint64, limit int) (core.LogChunk, error) {
	return call(s, ctx, func(st store.Store, ctx context.Context) (core.LogChunk, error) {
		return st.ReadLog(ctx, id, offset, limit)
	})
}

// Forgotten is what giving a repository up did.
type Forgotten struct {
	// Tasks is the tasks whose stores the daemon forgot.
	Tasks []string
	// Left is the jobs whose supervisors may still run, and which only a reboot or their death
	// lets the daemon clear.
	Left []string
}

// ForgetRepository gives up a repository that is gone for good. Its commands are ended and their
// supervisors told to go, then the machine forgets its jobs and the stores leave the index, so
// nothing of it waits or holds anything. A command that does not end in time is reported left, and
// holds the machine while it can run, for at most its limit. Nothing on disk is touched where the
// repository was. A repository that is there or cannot be read, or that the daemon knows nothing
// of, is refused, as forgetting one is not undone.
func (a *App) ForgetRepository(ctx context.Context, repository string) (Forgotten, error) {
	scopes, runs, err := a.ofRepository(ctx, repository)
	if err != nil {
		return Forgotten{}, err
	}
	defer func() {
		for _, run := range runs {
			if h := handleOf(run); h != nil {
				h.Close()
			}
		}
	}()
	if len(scopes) == 0 {
		return Forgotten{}, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("the daemon knows of no repository %s", repository)}
	}
	for _, scope := range scopes {
		_, err := a.cfg.Stores.Find(ctx, scope)
		switch {
		case err == nil:
			return Forgotten{}, &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
				"the repository %s is still there, with task %s: only a repository that is gone can be forgotten", repository, scope.Task)}
		case ctx.Err() != nil:
			return Forgotten{}, ctx.Err()
		case errors.Is(err, store.ErrNoStore), errors.Is(err, store.ErrGone):
			// Its place is not there: gone, as far as anyone can tell from here.
		default:
			// A store that cannot be reached, or that another daemon holds, may be whole: forgetting
			// it would let its jobs go for good.
			return Forgotten{}, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
				"the repository %s cannot be told gone, as task %s's store answered: %v; forget only a repository whose directory is not there",
				repository, scope.Task, err)}
		}
	}
	// The runs end first, while the machine still holds their jobs: a job it let go of before its
	// command ended would let the next one start beside it. A run that does not end keeps holding
	// the machine, as long as its command can run.
	var out Forgotten
	left := map[lifecycle.StoreID][]lifecycle.LeftRun{}
	for _, run := range runs {
		if err := a.endRun(ctx, run); err != nil {
			a.cfg.Logf("job %s: letting its supervisor go: %v", run.Job(), err)
			out.Left = append(out.Left, run.Job().String())
			ref := run.Job()
			left[ref.Store] = append(left[ref.Store], lifecycle.LeftRun{Ref: ref, Supervisor: a.presenceOf(run)})
		}
	}
	for _, scope := range scopes {
		id := storeIDOf(scope)
		a.settle(message{event: lifecycle.StoreForgotten{Store: id, Left: left[id], At: a.now()}})
	}
	for _, scope := range scopes {
		if err := a.cfg.Stores.Forget(ctx, scope); err != nil {
			return out, err
		}
		out.Tasks = append(out.Tasks, scope.Task)
	}
	a.notifyJobs()
	return out, nil
}

// ofRepository returns the stores and the runs of a repository, by what the index and the runs say.
func (a *App) ofRepository(ctx context.Context, repository string) ([]Scope, []supervisor.Run, error) {
	if !path.IsAbs(repository) {
		return nil, nil, invalid("repository: %q is not an absolute path", repository)
	}
	known, err := a.cfg.Stores.Known(ctx)
	if err != nil {
		return nil, nil, err
	}
	var scopes []Scope
	for _, scope := range known {
		if scope.Repository == repository {
			scopes = append(scopes, scope)
		}
	}
	var all []supervisor.Run
	err = a.call(ctx, func(ctx context.Context) error {
		var err error
		all, err = a.cfg.Supervisors.Runs(ctx)
		return err
	})
	if err != nil {
		return nil, nil, err
	}
	var runs []supervisor.Run
	for _, run := range all {
		scope, ok := scopeOfRun(run)
		switch {
		case ok && scope.Repository == repository:
			runs = append(runs, run)
			if !slices.Contains(scopes, scope) {
				scopes = append(scopes, scope)
			}
		case handleOf(run) != nil:
			handleOf(run).Close()
		}
	}
	return scopes, runs, nil
}

// endRun ends what is left of a run of a repository that is gone. A live command is cancelled and
// given its time to end. A run that may live and cannot be reached stays.
func (a *App) endRun(ctx context.Context, run supervisor.Run) error {
	switch r := run.(type) {
	case supervisor.Dead:
		return a.forget(ctx, r.Ref, r.Proof)
	case supervisor.Alive:
		if err := a.call(ctx, func(ctx context.Context) error { return r.Handle.Cancel(ctx, "its repository was forgotten") }); err != nil {
			if _, gone := goneProof(err); !gone {
				return err
			}
		}
		stop, done := clock.Deadline(ctx, a.cfg.Timers, a.cfg.StopTimeout)
		defer done()
		_, err := r.Handle.Follow(stop, pastTheEnd, func([]byte) error { return nil })
		if _, gone := goneProof(err); err != nil && !gone {
			return err
		}
		return a.release(ctx, r.Ref, r.Handle)
	case supervisor.Unknown:
		return errMayLive
	case supervisor.None:
		return nil
	}
	return errMayLive
}

// errMayLive says nothing proves a supervisor gone.
var errMayLive = errors.New("what is left of its supervisor may still live")
