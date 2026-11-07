package search

import (
	"cmp"
	"maps"
	"slices"
	"time"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// This file reads a view's facts out as plain records, so that what works on the job log (the window
// facts of a check-in) never speaks the job service's contract. Like Cap, it reads the uncapped view.

// Measurement is one measure result of a job a run submitted: when it happened, the candidate it
// measured, and the value of each objective it reports.
type Measurement struct {
	Job    core.JobID
	Commit core.Commit
	// At is when the result was recorded, or when its job finished if the result says nothing.
	At     time.Time
	Values []Value
}

// Value is what a measurement says of one objective.
type Value struct {
	Metric    string
	Direction core.Direction
	Number    float64
	// Best says the value is better than every earlier measurement's of the metric, in its direction,
	// the earliest of equal values keeping it: the best so far steps here. It is the mark the series
	// of Trajectory carries, from the same projection.
	Best bool
}

// Measurements are the run's measure results, once for each job, oldest first (by time, then job),
// as the one projection of them says (measurements): the same results, times, directions and best
// marks the view's trajectory draws, so what a check-in freezes is what the report shows. A result
// that is no measurement, whose job is not the run's, or that cannot be placed in time is left out.
// Each measurement's values are by metric name.
func (v View) Measurements(run core.RunID) []Measurement {
	var out []Measurement
	for _, m := range measurements(v.Facts, string(run)) {
		values := make([]Value, 0, len(m.values))
		for _, name := range slices.Sorted(maps.Keys(m.values)) {
			x := m.values[name]
			values = append(values, Value{Metric: name, Direction: directions[x.direction], Number: x.value, Best: x.best})
		}
		out = append(out, Measurement{Job: core.JobID(m.jobID), Commit: core.Commit(m.commit), At: m.at, Values: values})
	}
	return out
}

var directions = map[cli.Direction]core.Direction{
	cli.Direction_DIRECTION_MAXIMIZE: core.Maximize,
	cli.Direction_DIRECTION_MINIMIZE: core.Minimize,
}

// Plan is when a plan was made and last edited. The plans of a view are those of the whole task: a
// plan belongs to a repository and its task, not to a run. A time the plan does not state is the zero
// time.
type Plan struct {
	Number               uint32
	CreatedAt, UpdatedAt time.Time
}

// Plans are the task's plans, ascending by number, as the view holds them.
func (v View) Plans() []Plan {
	var out []Plan
	for _, f := range v.Facts {
		if p := f.GetPlan(); p != nil && p.GetNumber() != 0 {
			plan := Plan{Number: p.GetNumber()}
			if p.GetCreatedAt() != nil {
				plan.CreatedAt = p.GetCreatedAt().AsTime()
			}
			if p.GetUpdatedAt() != nil {
				plan.UpdatedAt = p.GetUpdatedAt().AsTime()
			}
			out = append(out, plan)
		}
	}
	slices.SortStableFunc(out, func(a, b Plan) int { return cmp.Compare(a.Number, b.Number) })
	return out
}
