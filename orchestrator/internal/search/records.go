package search

import (
	"cmp"
	"slices"
	"time"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// This file reads a view's facts out as plain records, so that what works on the job log (the window
// facts of a check-in) never speaks the job service's contract. Like Cap, it reads the uncapped view.

// Measurement is one measure result of a job a run submitted: when it was recorded, the candidate it
// measured, and the value of each objective it scores.
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
}

// Measurements are the measure results of the jobs the run submitted, once for each job, in the
// order of the view. A job counts as the run's when the run is among its submitters, as Cap decides
// it. A result that is no measurement, whose job is not the run's, or that names no time for itself
// or its job is left out: it cannot be placed in a window. An objective that names no direction has
// no value, because "better" would be a guess.
func (v View) Measurements(run core.RunID) []Measurement {
	jobs := map[string]*cli.Job{}
	for _, f := range v.Facts {
		if j := f.GetJob(); j != nil && submittedBy(j, string(run)) {
			jobs[j.GetId()] = j
		}
	}
	var out []Measurement
	seen := map[string]bool{}
	for _, f := range v.Facts {
		r := f.GetResult()
		job, ok := jobs[r.GetJobId()]
		m := r.GetMeasure()
		if !ok || m == nil || seen[r.GetJobId()] {
			continue
		}
		var at time.Time
		switch {
		case r.GetRecordedAt() != nil:
			at = r.GetRecordedAt().AsTime()
		case job.GetFinishedAt() != nil:
			at = job.GetFinishedAt().AsTime()
		default:
			continue
		}
		seen[r.GetJobId()] = true
		out = append(out, Measurement{Job: core.JobID(r.GetJobId()), Commit: core.Commit(r.GetCommit()), At: at, Values: valuesOf(m)})
	}
	return out
}

// valuesOf reads the value of each objective a measurement scores. A task with one objective has the
// scored metric's value. A task with several has each objective's: the scored metric's own value, and
// for each other the headline of the observation of its name, which is absent when it has no values.
func valuesOf(m *cli.MeasureResult) []Value {
	var out []Value
	add := func(name string, d cli.Direction, number float64) {
		if dir, ok := directions[d]; ok && name != "" {
			out = append(out, Value{Metric: name, Direction: dir, Number: number})
		}
	}
	scored := m.GetMetric().GetName()
	if metrics := m.GetObjectives().GetMetrics(); len(metrics) > 1 {
		for _, metric := range metrics {
			if metric.GetName() == scored {
				add(scored, metric.GetDirection(), m.GetValue())
				continue
			}
			for _, o := range m.GetObservations() {
				if o.GetName() == metric.GetName() && o.Value != nil {
					add(metric.GetName(), metric.GetDirection(), o.GetValue())
				}
			}
		}
		return out
	}
	add(scored, m.GetMetric().GetDirection(), m.GetValue())
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
