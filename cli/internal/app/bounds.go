package app

import (
	"context"
	"errors"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// The bound flows. A bound is an agent's statement of the best an objective of the task can reach.
// The daemon checks it names an objective the task declares, takes the objective's direction from
// the task, stamps who declared it and when from the call and its own clock, and appends it to the
// task's log. It never overwrites an earlier declaration, and compares the bound with nothing. A
// declaration makes the task's store if it has none, as a plan does: agents reason about the bound
// before the first job runs. Listing makes nothing, and a task with no store has no bounds.

// DeclareBound records a bound the call's agent states, read against the task as the commit has it.
func (a *App) DeclareBound(ctx context.Context, c core.CallContext, commit core.Commit, statement core.BoundStatement) (core.Bound, error) {
	if !commit.Valid() {
		return core.Bound{}, invalid("commit: %q is not a full 40-digit commit id", commit)
	}
	scope := c.Scope
	if err := resolved(scope); err != nil {
		return core.Bound{}, err
	}
	t, err := a.taskAt(ctx, string(scope.Repository()), commit, string(scope.Task()))
	if err != nil {
		return core.Bound{}, err
	}
	decided, err := core.DecideBound(t.Declared(), statement, core.RecordOrigin{Run: c.Run, Agent: c.Agent, At: a.now()})
	if err != nil {
		return core.Bound{}, err
	}
	st, err := a.createStore(ctx, scope)
	if err != nil {
		return core.Bound{}, err
	}
	return st.DeclareBound(ctx, decided)
}

// Bounds returns every bound declared for a task, oldest first.
func (a *App) Bounds(ctx context.Context, scope core.Scope) ([]core.Bound, error) {
	st, err := a.open(ctx, scope)
	if errors.Is(err, store.ErrNoStore) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return st.Bounds(ctx)
}
