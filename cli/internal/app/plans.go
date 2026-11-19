package app

import (
	"context"
	"errors"
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// The plan flows. A plan belongs to a task, like a job, and the daemon keeps no state for it
// beyond the store: every call is one atomic store operation, so a plan written by a call that
// returned is durable, and a call that failed changed nothing. Which status a plan takes, and in
// what order, is the agents' to decide: the daemon refuses only a value that is not a status.
// A new plan makes the task's store if it has none, like a submission, because planners write
// plans before the first job runs. Every other plan flow makes nothing: on a task with no store it
// is refused as not found, and a list of its plans is empty.

// PlanChange is the fields an edit sets. A nil field is left as it is.
type PlanChange struct {
	Title     *string
	Body      *string
	Status    *core.PlanStatus
	GainBound *core.GainBound
	// Workstream moves the plan to that workstream, and 0 leaves it where it is.
	Workstream core.WorkstreamNumber
	// Kind replaces the plan's kind.
	Kind *core.PlanKind
	// DependsOn replaces the plan's dependencies, all of them; an empty list clears them.
	DependsOn *[]core.PlanNumber
}

// PlanDraft is what a new plan states. The zero value of each field but the title is none stated.
type PlanDraft struct {
	Title     string
	Body      string
	GainBound *core.GainBound
	// Workstream is the workstream the plan joins, 0 for none.
	Workstream core.WorkstreamNumber
	// Kind is what kind of change the plan makes, "" for none.
	Kind core.PlanKind
	// DependsOn are the plans it builds on.
	DependsOn []core.PlanNumber
}

// CreatePlan stores a new open plan with the next number, in the workstream when it names one. A
// workstream or a plan it depends on that the task does not have is refused as not found.
func (a *App) CreatePlan(ctx context.Context, c core.CallContext, draft PlanDraft) (core.Plan, error) {
	if err := core.CheckPlanTitle("title", draft.Title); err != nil {
		return core.Plan{}, err
	}
	if err := core.CheckPlanBody("body", draft.Body); err != nil {
		return core.Plan{}, err
	}
	if draft.GainBound != nil {
		if err := core.CheckGainBound("gain_bound", *draft.GainBound); err != nil {
			return core.Plan{}, err
		}
	}
	if draft.Kind != "" && !draft.Kind.Valid() {
		return core.Plan{}, invalid("kind: %q is not a plan kind", draft.Kind)
	}
	scope := c.Scope
	if err := resolved(scope); err != nil {
		return core.Plan{}, err
	}
	st, err := a.createStore(ctx, scope)
	if err != nil {
		return core.Plan{}, err
	}
	return st.CreatePlan(ctx, core.NewPlan{
		Title: draft.Title, Body: draft.Body, GainBound: draft.GainBound, Workstream: draft.Workstream,
		Kind: draft.Kind, DependsOn: draft.DependsOn, Origin: a.originOf(c),
	})
}

// originOf is the origin a record the call makes has: the run and agent the call carried, and the
// daemon's own clock now. A request carries neither the origin nor its time, so no caller states
// another's.
func (a *App) originOf(c core.CallContext) core.RecordOrigin {
	return core.RecordOrigin{Run: c.Run, Agent: c.Agent, At: a.now()}
}

// Plans returns the plans of a task that match the filter, by number. A plan is ready while it is
// open and every plan it depends on is won (core.Plan.Ready).
func (a *App) Plans(ctx context.Context, scope core.Scope, filter core.PlanFilter) ([]core.Plan, error) {
	for _, s := range filter.Statuses {
		if !s.Valid() {
			return nil, invalid("statuses: %q is not a plan status", s)
		}
	}
	for _, k := range filter.Kinds {
		if !k.Valid() {
			return nil, invalid("kinds: %q is not a plan kind", k)
		}
	}
	st, err := a.open(ctx, scope)
	if errors.Is(err, store.ErrNoStore) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return st.Plans(ctx, filter)
}

// ViewPlan returns a plan with its comments, the jobs linked to it, oldest first, and the plans it
// depends on and that depend on it, by number.
func (a *App) ViewPlan(ctx context.Context, scope core.Scope, number core.PlanNumber) (core.PlanView, error) {
	st, err := a.open(ctx, scope)
	if err != nil {
		return core.PlanView{}, err
	}
	plan, err := st.Plan(ctx, number)
	if err != nil {
		return core.PlanView{}, err
	}
	jobs, err := st.Jobs(ctx, core.JobFilter{})
	if err != nil {
		return core.PlanView{}, err
	}
	plans, err := st.Plans(ctx, core.PlanFilter{})
	if err != nil {
		return core.PlanView{}, err
	}
	view := core.PlanView{Plan: plan, Dependents: core.Dependents(number, plans)}
	for _, p := range plans {
		if slices.Contains(plan.DependsOn, p.Number) {
			view.DependsOn = append(view.DependsOn, p)
		}
	}
	view.Jobs = core.LinkedTo(number, jobs)
	view.Verdicts = a.verdictsOf(ctx, taskStore{scope: scope, store: st}, view.Jobs)
	return view, nil
}

// EditPlan changes a plan's title, body or status as one change. An edit that sets nothing is
// refused, since it would only move the update time.
func (a *App) EditPlan(ctx context.Context, c core.CallContext, number core.PlanNumber, change PlanChange) (core.Plan, error) {
	if change.Title == nil && change.Body == nil && change.Status == nil && change.GainBound == nil && change.Workstream == 0 &&
		change.Kind == nil && change.DependsOn == nil {
		return core.Plan{}, invalid("edit: set at least one of title, body, status, gain_bound, workstream, kind or depends_on")
	}
	if change.Kind != nil && !change.Kind.Valid() {
		return core.Plan{}, invalid("kind: %q is not a plan kind", *change.Kind)
	}
	if change.GainBound != nil {
		if err := core.CheckGainBound("gain_bound", *change.GainBound); err != nil {
			return core.Plan{}, err
		}
	}
	if change.Title != nil {
		if err := core.CheckPlanTitle("title", *change.Title); err != nil {
			return core.Plan{}, err
		}
	}
	if change.Body != nil {
		if err := core.CheckPlanBody("body", *change.Body); err != nil {
			return core.Plan{}, err
		}
	}
	if change.Status != nil && !change.Status.Valid() {
		return core.Plan{}, invalid("status: %q is not a plan status", *change.Status)
	}
	edit := core.PlanEdit{Title: change.Title, Body: change.Body, Status: change.Status, GainBound: change.GainBound, Kind: change.Kind, DependsOn: change.DependsOn}
	if change.Workstream != 0 {
		edit.Workstream = &change.Workstream
	}
	return a.editPlan(ctx, c, number, edit)
}

// CommentPlan adds a comment, which carries a decision or a result.
func (a *App) CommentPlan(ctx context.Context, c core.CallContext, number core.PlanNumber, text string) (core.Plan, error) {
	if err := core.CheckPlanComment("text", text); err != nil {
		return core.Plan{}, err
	}
	return a.editPlan(ctx, c, number, core.PlanEdit{Comment: &core.PlanComment{Text: text, At: a.now(), Agent: c.Agent}})
}

// ClosePlan sets a status other than open and adds a comment when there is one, as one change.
func (a *App) ClosePlan(ctx context.Context, c core.CallContext, number core.PlanNumber, status core.PlanStatus, comment string) (core.Plan, error) {
	if !status.Closes() {
		return core.Plan{}, invalid("status: %q does not close a plan (chosen, won, parked or rejected)", status)
	}
	edit := core.PlanEdit{Status: &status}
	if comment != "" {
		if err := core.CheckPlanComment("comment", comment); err != nil {
			return core.Plan{}, err
		}
		edit.Comment = &core.PlanComment{Text: comment, At: a.now(), Agent: c.Agent}
	}
	return a.editPlan(ctx, c, number, edit)
}

func (a *App) editPlan(ctx context.Context, c core.CallContext, number core.PlanNumber, edit core.PlanEdit) (core.Plan, error) {
	st, err := a.open(ctx, c.Scope)
	if err != nil {
		return core.Plan{}, err
	}
	edit.Origin = a.originOf(c)
	return st.EditPlan(ctx, number, edit)
}
