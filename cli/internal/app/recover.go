package app

import (
	"context"
	"fmt"
	"maps"
	"slices"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// Start tells the machine what the stores and the supervisors hold, and so starts the daemon's
// work: queued jobs wait their turn again, a running job whose supervisor answers is followed
// where it left off, one whose supervisor is dead is lost, and a run no job needs is cleared. A
// first start finds nothing and is the same code.
//
// It fails, and the daemon does not serve, only when the list of stores or of supervisors cannot be
// read: without them it cannot tell what it would be touching. A store that cannot be read, such
// as a repository on a disk that is not mounted, is left out for now and does not stop the rest.
// A run of such a store is not released and its outcome is not discarded: the run is in doubt and
// holds the machine, the store is tried again, and when it returns the run is taken up.
func (a *App) Start(ctx context.Context) error {
	var scopes []Scope
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
		byRef[r.Ref] = r
	}
	away := map[Scope]bool{}
	for _, scope := range scopes {
		if err := a.recoverScope(ctx, scope, byRef); err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			a.cfg.Logf("the store of task %s in %s cannot be read yet: %v", scope.Task, scope.Repository, err)
			away[scope] = true
		}
	}
	// What is left is runs of jobs the stores do not have as queued or running, and runs of stores
	// that could not be read.
	for _, ref := range sortedRefs(byRef) {
		r := byRef[ref]
		scope := Scope{Repository: r.Launch.Repository, Task: r.Launch.Task}
		found := lifecycle.Recovered{Ref: ref, Supervisor: presenceOf(r), At: a.now()}
		switch {
		case r.Launch.Repository == "":
			found.Away = false
		case away[scope]:
			found.Away = true
		default:
			if !slices.Contains(scopes, scope) {
				// The store is not in the index, so nothing was recorded for it, or the index
				// lost it. Reading it says which.
				found = a.lookInStore(ctx, found, scope)
				if found.Away {
					away[scope] = true
				}
			}
		}
		if r.Launch.Repository == "" {
			a.post(message{event: found, handle: r.Handle})
			continue
		}
		a.post(message{event: found, handle: r.Handle, scope: &scope})
	}
	a.post(message{event: lifecycle.RecoveryDone{}, then: func() { a.sweep(named(a.ctx, "sweep"), away) }})
	return nil
}

func sortedRefs(runs map[lifecycle.Ref]supervisor.Run) []lifecycle.Ref {
	out := slices.Collect(maps.Keys(runs))
	slices.SortFunc(out, func(a, b lifecycle.Ref) int { return strings.Compare(a.String(), b.String()) })
	return out
}

// presenceOf is what a listing says of a run: no entry is PresenceNone.
func presenceOf(r supervisor.Run) lifecycle.Presence {
	if r.Presence == "" {
		return lifecycle.PresenceNone
	}
	return r.Presence
}

// recoverScope tells the machine of the unfinished jobs of one store, with what is left of their
// runs, and takes those runs from byRef.
func (a *App) recoverScope(ctx context.Context, scope Scope, byRef map[lifecycle.Ref]supervisor.Run) error {
	st, err := a.cfg.Stores.Open(ctx, scope)
	if err != nil {
		return err
	}
	jobs, err := st.Jobs(ctx, core.JobFilter{States: []core.JobState{core.JobQueued, core.JobRunning}})
	if err != nil {
		return err
	}
	for _, job := range jobs {
		ref := lifecycle.Ref{Store: storeIDOf(scope), Job: job.ID}
		run := byRef[ref]
		delete(byRef, ref)
		a.post(message{
			event: lifecycle.Recovered{
				Ref: ref, State: job.State, SubmittedAt: job.SubmittedAt, Supervisor: presenceOf(run), At: a.now(),
			},
			handle: run.Handle, scope: &scope,
		})
	}
	return nil
}

func storeIDOf(s Scope) lifecycle.StoreID { return lifecycle.StoreIDOf(s.Repository, s.Task) }

// sweep looks again, every slow period, at what no event will bring: a store that was away, and a
// queued job the machine never heard of, because a submission reached the store and its answer
// did not reach the daemon.
func (a *App) sweep(ctx context.Context, away map[Scope]bool) {
	for a.sleep(ctx, a.cfg.SlowPeriod) {
		a.sweepOnce(ctx, away)
	}
}

func (a *App) sweepOnce(ctx context.Context, away map[Scope]bool) {
	scopes, err := a.cfg.Stores.Known(ctx)
	if err != nil {
		a.cfg.Logf("listing the stores: %v", err)
		return
	}
	for _, scope := range scopes {
		if away[scope] {
			var runs []supervisor.Run
			err := a.call(ctx, func(ctx context.Context) error {
				var err error
				runs, err = a.cfg.Supervisors.Runs(ctx)
				return err
			})
			if err != nil {
				a.cfg.Logf("listing the supervisors: %v", err)
				continue
			}
			byRef := map[lifecycle.Ref]supervisor.Run{}
			for _, r := range runs {
				byRef[r.Ref] = r
			}
			if a.recoverScope(ctx, scope, byRef) == nil {
				delete(away, scope)
			}
			continue
		}
		st, err := a.cfg.Stores.Open(ctx, scope)
		if err != nil {
			continue
		}
		jobs, err := st.Jobs(ctx, core.JobFilter{States: []core.JobState{core.JobQueued}})
		if err != nil {
			continue
		}
		for _, job := range jobs {
			a.post(message{
				event: lifecycle.Admitted{Ref: lifecycle.Ref{Store: storeIDOf(scope), Job: job.ID}, SubmittedAt: job.SubmittedAt},
				scope: &scope,
			})
		}
	}
}
