package app

import (
	"context"
	"errors"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// The workstream flows. A workstream is a line of work an agent names, which plans join. Like a
// plan it belongs to a task and the daemon keeps nothing for it beyond the store: every call is one
// atomic store operation. Starting one makes the task's store if it has none, since planners name
// their lines of work before the first job runs; listing makes nothing, and a task with no store has
// no workstreams.

// WorkstreamChange is the fields an edit sets. A nil field is left as it is.
type WorkstreamChange struct {
	Title  *string
	Status *core.WorkstreamStatus
	// Note replaces the note, and an empty one clears it.
	Note *string
}

// StartWorkstream starts the workstream the title names, or finds the one that is not closed with
// that title, ignoring case and white space. The store decides which, so agents that start one line
// of work at the same moment get one number.
func (a *App) StartWorkstream(ctx context.Context, c core.CallContext, title string) (core.StartedWorkstream, error) {
	stored, err := core.DecideWorkstreamTitle("title", title)
	if err != nil {
		return core.StartedWorkstream{}, err
	}
	if err := resolved(c.Scope); err != nil {
		return core.StartedWorkstream{}, err
	}
	st, err := a.createStore(ctx, c.Scope)
	if err != nil {
		return core.StartedWorkstream{}, err
	}
	return st.StartWorkstream(ctx, core.NewWorkstream{Title: stored, Origin: a.originOf(c)})
}

// Workstreams returns the workstreams of a task that match the filter, by number.
func (a *App) Workstreams(ctx context.Context, scope core.Scope, filter core.WorkstreamFilter) ([]core.Workstream, error) {
	for _, s := range filter.Statuses {
		if !s.Valid() {
			return nil, invalid("statuses: %q is not a workstream status", s)
		}
	}
	st, err := a.open(ctx, scope)
	if errors.Is(err, store.ErrNoStore) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return st.Workstreams(ctx, filter)
}

// EditWorkstream changes a workstream's title, status or note as one change. An edit that sets
// nothing is refused. One that sets what the workstream already has changes nothing and says so by
// leaving its update time as it was.
func (a *App) EditWorkstream(ctx context.Context, c core.CallContext, number core.WorkstreamNumber, change WorkstreamChange) (core.Workstream, error) {
	if change.Title == nil && change.Status == nil && change.Note == nil {
		return core.Workstream{}, invalid("edit: set at least one of title, status or note")
	}
	edit := core.WorkstreamEdit{Status: change.Status, Note: change.Note, At: a.now()}
	if change.Title != nil {
		title, err := core.DecideWorkstreamTitle("title", *change.Title)
		if err != nil {
			return core.Workstream{}, err
		}
		edit.Title = &title
	}
	if change.Status != nil && !change.Status.Valid() {
		return core.Workstream{}, invalid("status: %q is not a workstream status", *change.Status)
	}
	if change.Note != nil {
		if err := core.CheckWorkstreamNote("note", *change.Note); err != nil {
			return core.Workstream{}, err
		}
	}
	st, err := a.open(ctx, c.Scope)
	if err != nil {
		return core.Workstream{}, err
	}
	return st.EditWorkstream(ctx, number, edit)
}
