package app

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// Start tells the machine what the stores and the supervisors hold, and so starts the daemon's
// work: queued jobs wait their turn again, a running job whose supervisor answers is followed
// where it left off, one whose supervisor is proved dead is lost, and a run no job needs is cleared.
// A first start finds nothing and is the same code.
//
// It fails, and the daemon does not serve, only when the list of stores or of supervisors cannot be
// read: without them it cannot tell what it would be touching. A store that cannot be read, such
// as a repository on a disk that is not mounted, stays known and does not stop the rest: none of
// its jobs starts, a run of it is not released and its outcome is not discarded, the store is tried
// again, and when it returns the run is taken up. The same goes for a store another daemon has.
func (a *App) Start(ctx context.Context) error {
	var scopes []core.Scope
	err := a.retry(ctx, func(ctx context.Context) error {
		var err error
		scopes, err = a.cfg.Stores.Known(ctx)
		return err
	})
	if err != nil {
		return fmt.Errorf("listing the stores: %w", err)
	}
	var runs []supervisor.Run
	err = a.call(ctx, func(ctx context.Context) error {
		var err error
		runs, err = a.cfg.Supervisors.Runs(ctx)
		return err
	})
	if err != nil {
		return fmt.Errorf("listing the supervisors: %w", err)
	}
	byRef := map[lifecycle.Ref]supervisor.Run{}
	for _, r := range runs {
		byRef[r.Job()] = r
	}
	away := map[lifecycle.StoreID]bool{}
	take := func(ref lifecycle.Ref) supervisor.Run {
		run, ok := byRef[ref]
		if !ok {
			return supervisor.None{Ref: ref}
		}
		delete(byRef, ref)
		return run
	}
	for _, scope := range scopes {
		scopeCtx := a.observationContext(ctx, storeIDOf(scope))
		if err := a.recoverScope(scopeCtx, scope, take); err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			a.cfg.Logf("the store of task %s in %s cannot be read yet: %v", string(scope.Task()), string(scope.Repository()), err)
			a.markUnread(scopeCtx, scope, err)
			away[storeIDOf(scope)] = true
		}
	}
	// What is left is runs of jobs the stores do not have as queued or running, and runs of stores
	// that could not be read.
	for _, ref := range sortedRefs(byRef) {
		r := byRef[ref]
		ctx := a.observationContext(ctx, ref.Store)
		found := lifecycle.Recovered{Ref: ref, Finding: lifecycle.NoSuchJob{}, Supervisor: a.presenceOf(r), At: a.now()}
		m := message{handle: handleOf(r)}
		scope, known := scopeOfRun(r)
		switch {
		case away[ref.Store]:
			found.Finding = lifecycle.StoreAway{}
		case known && !slices.Contains(scopes, scope):
			// The store is not in the index, so nothing was recorded for it, or the index lost it.
			// Reading it says which.
			found.Finding = a.lookInStore(ctx, ref, scope, found.Supervisor)
			if _, unread := found.Finding.(lifecycle.StoreAway); unread {
				a.markUnread(ctx, scope, errNoRead)
				away[ref.Store] = true
			}
		}
		if known {
			m.scope = &scope
		}
		m.event = found
		a.postEffect(ctx, m)
	}
	a.settleBranches(ctx, scopes)
	a.post(message{event: lifecycle.RecoveryDone{}, then: func() { a.sweep(named(a.ctx, "sweep")) }})
	return nil
}

// errNoRead says a store did not answer a look at one job.
var errNoRead = &core.Failure{Kind: core.FailureUnavailable, Message: "it could not be read"}

// markUnread tells the machine a store could not be read, so that none of its jobs starts.
func (a *App) markUnread(ctx context.Context, scope core.Scope, err error) {
	a.postEffect(ctx, message{event: lifecycle.StoreUnread{Store: storeIDOf(scope), Failure: failureOf(err)}, scope: &scope})
}

func sortedRefs(runs map[lifecycle.Ref]supervisor.Run) []lifecycle.Ref {
	out := slices.Collect(maps.Keys(runs))
	slices.SortFunc(out, func(a, b lifecycle.Ref) int { return strings.Compare(a.String(), b.String()) })
	return out
}

// recoverScope tells the machine of the unfinished jobs of one store, each with what runOf finds
// of its run.
func (a *App) recoverScope(ctx context.Context, scope core.Scope, runOf func(lifecycle.Ref) supervisor.Run) error {
	ctx = a.observationContext(ctx, storeIDOf(scope))
	// Most failures here wait for the slow sweep, and hold up nothing. A daemon that died a moment
	// ago is different: its store is free as soon as the kernel gets to it, and its jobs are this
	// daemon's to answer for.
	var st store.Store
	err := a.retryWhile(ctx, ownerLeaving, func(ctx context.Context) error {
		var err error
		st, err = a.cfg.Stores.Find(ctx, scope)
		return err
	})
	if err != nil {
		return err
	}
	jobs, err := st.Jobs(ctx, core.JobFilter{States: []core.JobState{core.JobQueued, core.JobRunning}})
	if err != nil {
		return err
	}
	given, err := a.cfg.Stores.GivenUp(ctx, scope)
	if err != nil {
		return err
	}
	for _, job := range jobs {
		ref := lifecycle.Ref{Store: storeIDOf(scope), Job: job.ID}
		run := runOf(ref)
		presence := a.presenceOf(run)
		a.postEffect(ctx, message{
			event: lifecycle.Recovered{
				Ref: ref, Finding: a.jobIn(ctx, scope, job, presence, given),
				Supervisor: presence, At: a.now(),
			},
			handle: handleOf(run), scope: &scope,
		})
	}
	return nil
}

func ownerLeaving(err error) bool { return errors.Is(err, store.ErrOwnerLeaving) }

// admittedOf is the event that a queued job wants to run.
func (a *App) admittedOf(ref lifecycle.Ref, job core.Job) lifecycle.Admitted {
	return lifecycle.Admitted{
		Ref: ref, SubmittedAt: job.SubmittedAt, Attempts: len(job.Attempts), Policy: core.RerunPolicyOf(job.Spec.Settings.Kind()),
		Work: a.workOf(job),
	}
}

// workOf is what a job asks of the machine: where it may run and what it claims there, as the
// demand says of the job in the environment it runs in, and the run that submitted it first, whose
// use of the machine it counts as.
func (a *App) workOf(job core.Job) lifecycle.Work {
	job.Spec.Environment = queuedEnvironment(job.Spec)
	w := lifecycle.Work{Options: a.cfg.Scheduler.Demand.Options(job), Kind: job.Spec.Settings.Kind()}
	if len(job.Submitters) > 0 {
		w.Run = job.Submitters[0].Run
	}
	return w
}

// queuedEnvironment is the environment a queued job runs in. A submission decides it before the job
// is queued (keyFlow, placeFlow), so only a command job queued by an older daemon, which left it
// unspecified, has none. Such a job runs on the host, as it would have then. It is decided here
// alone, so the claims a job is scheduled by (workOf) and the environment it is launched in
// (prepare) are the same.
func queuedEnvironment(spec core.JobSpec) core.EnvironmentKind {
	if spec.Environment == core.EnvironmentAuto {
		return core.EnvironmentHost
	}
	return spec.Environment
}

func storeIDOf(s core.Scope) lifecycle.StoreID {
	return lifecycle.StoreIDOf(string(s.Repository()), string(s.Task()))
}

// sweep looks again, every slow period, at what no event will bring: a store that could not be
// read, a queued job the machine never heard of, because a submission reached the store and its
// answer did not reach the daemon, and a running job the machine does not hold, which is a job it
// let go of when its store could not say what it was.
func (a *App) sweep(ctx context.Context) {
	for a.sleep(ctx, a.cfg.SlowPeriod) {
		a.sweepOnce(ctx)
	}
}

func (a *App) sweepOnce(ctx context.Context) {
	scopes, err := a.cfg.Stores.Known(ctx)
	if err != nil {
		a.cfg.Logf("listing the stores: %v", err)
	}
	unread := a.view.Load().unread
	for _, scope := range unread {
		if !slices.Contains(scopes, scope) {
			scopes = append(scopes, scope)
		}
	}
	for _, scope := range scopes {
		if !slices.Contains(unread, scope) {
			a.sweepStore(ctx, scope)
			continue
		}
		a.readAgain(ctx, scope)
	}
}

// readAgain takes up a store that could not be read before.
func (a *App) readAgain(ctx context.Context, scope core.Scope) error {
	ctx = a.observationContext(ctx, storeIDOf(scope))
	err := a.recoverScope(ctx, scope, func(ref lifecycle.Ref) supervisor.Run { return a.look(ctx, ref) })
	if err != nil {
		a.cfg.Logf("the store of task %s in %s cannot be read yet: %v", string(scope.Task()), string(scope.Repository()), err)
		return err
	}
	a.postEffect(ctx, message{event: lifecycle.StoreRead{Store: storeIDOf(scope)}, readiness: storeReadable})
	return nil
}

// sweepStore looks at a store the machine has read before.
func (a *App) sweepStore(ctx context.Context, scope core.Scope) {
	ctx = a.observationContext(ctx, storeIDOf(scope))
	st, err := a.cfg.Stores.Find(ctx, scope)
	var jobs []core.Job
	if err == nil {
		jobs, err = st.Jobs(ctx, core.JobFilter{States: []core.JobState{core.JobQueued, core.JobRunning}})
	}
	if err != nil {
		if ctx.Err() == nil {
			a.cfg.Logf("the store of task %s in %s cannot be read: %v", string(scope.Task()), string(scope.Repository()), err)
			a.markUnread(ctx, scope, err)
		}
		return
	}
	// Read after the jobs: a job leaves the machine after its store says it ended, so one the store
	// showed running and the machine does not hold has either ended since, which the fresh look
	// says, or was let go of.
	held := a.view.Load().held
	for _, job := range jobs {
		ref := lifecycle.Ref{Store: storeIDOf(scope), Job: job.ID}
		switch {
		case job.State == core.JobQueued:
			a.postEffect(ctx, message{event: a.admittedOf(ref, job), scope: &scope})
		case !held[ref]:
			run := a.look(ctx, ref)
			presence := a.presenceOf(run)
			a.postEffect(ctx, message{
				event:  lifecycle.Recovered{Ref: ref, Finding: a.lookInStore(ctx, ref, scope, presence), Supervisor: presence, At: a.now()},
				handle: handleOf(run), scope: &scope,
			})
		}
	}

	a.postEffect(ctx, message{event: lifecycle.StoreRead{Store: storeIDOf(scope)}, readiness: storeReadable})
}
