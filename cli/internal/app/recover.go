package app

import (
	"context"
	"fmt"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// Start tells the machine what the stores and the supervisors hold, and so starts the daemon's
// work: queued jobs wait their turn again, a running job whose supervisor answers is followed
// where it left off, one whose supervisor is gone is lost, and a run no job needs is cleared. A
// first start finds nothing and is the same code.
//
// It fails, and the daemon does not serve, when a store cannot be read: with a store missing from
// the picture, a running job in it would look like a stray run and be mishandled.
func (a *App) Start(ctx context.Context) error {
	scopes, err := a.cfg.Stores.Known(ctx)
	if err != nil {
		return fmt.Errorf("listing the stores: %w", err)
	}
	runs, err := a.cfg.Supervisors.Runs(ctx)
	if err != nil {
		return fmt.Errorf("listing the supervisors: %w", err)
	}
	byRef := map[lifecycle.Ref]supervisor.Run{}
	for _, r := range runs {
		byRef[r.Ref] = r
	}
	at := a.now()
	for _, scope := range scopes {
		t, err := a.open(ctx, scope)
		if err != nil {
			return fmt.Errorf("opening the store of task %s in %s: %w", scope.Task, scope.Repository, err)
		}
		jobs, err := t.store.Jobs(ctx, core.JobFilter{States: []core.JobState{core.JobQueued, core.JobRunning}})
		if err != nil {
			return fmt.Errorf("reading the jobs of task %s in %s: %w", scope.Task, scope.Repository, err)
		}
		for _, job := range jobs {
			ref := lifecycle.Ref{Store: lifecycle.StoreIDOf(scope.Repository, scope.Task), Job: job.ID}
			a.recovered(ref, job.State, job.SubmittedAt, at, byRef[ref])
			delete(byRef, ref)
		}
	}
	// What is left is runs of jobs the stores do not have as queued or running.
	for ref, r := range byRef {
		a.recovered(ref, "", at, at, r)
	}
	a.settleBranches(ctx, scopes)
	a.dispatch(lifecycle.RecoveryDone{})
	return nil
}

// recovered tells the machine of one job and what is left of its run. A run that has no entry in
// the listing is PresenceNone. The state is empty for a run no store job needs.
func (a *App) recovered(ref lifecycle.Ref, state core.JobState, submitted, at time.Time, run supervisor.Run) {
	presence := run.Presence
	if presence == "" {
		presence = lifecycle.PresenceNone
	}
	if presence == lifecycle.PresenceAlive && run.Handle != nil {
		a.setHandle(ref, run.Handle)
	}
	if state == core.JobQueued {
		// A leftover run of a queued job is cleared and the job goes on queueing: the machine
		// is told so with Discarded, not Closed.
		a.mu.Lock()
		a.discarded[ref] = true
		a.mu.Unlock()
	}
	a.dispatch(lifecycle.Recovered{Ref: ref, State: state, SubmittedAt: submitted, Supervisor: presence, At: at})
}
