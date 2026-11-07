package search

import (
	"cmp"
	"math"
	"slices"
	"time"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// This file is the one projection of the measure results of a run. What a screen draws of the
// measured history (Trajectory), what a check-in freezes of it (View.Measurements, which the window
// facts read) and what the head is compared with a bound by (Bounds) all read it, so each rule below
// is stated once and no two readers can disagree on it:
//
//   - Which results count: a measure result of a job the run submitted, which names the job and the
//     candidate it measured. The store holds the results of every run of the task.
//   - When it happened: when the result was recorded, or when its job finished if the result says
//     nothing. A result with neither cannot be placed and does not count.
//   - Once for each job: a stream may repeat a result, and a result is immutable, so the first of a
//     job's counts and the repeats do not.
//   - Which readings are objectives: the metric the measurement scores, the objectives its task
//     declares, and any other observation the run has a standing bound for (declaredBounds: the run's
//     own and a person's outside any run). The other readings are auxiliary. A reading that names no
//     direction, from the task or from its bound, is no objective: "better" would be a guess.
//   - Which way an objective improves: as its newest measurement says. The values of an older
//     measurement that said the other way (the task changed) are dropped, as no longer about it.
//   - The best so far: a value is the best so far when it is better than every earlier value of its
//     objective, the earliest of equal values keeping it. markBests is the only place it is decided.

// objectiveValue is one candidate's headline number on one objective, the way the measurement said
// the objective improves, and whether it is the best so far (set by markBests, over the whole
// history).
type objectiveValue struct {
	value     float64
	direction cli.Direction
	best      bool
}

// measured is one measure result of the run: the candidate, when it happened, and its value on each
// objective it reports.
type measured struct {
	jobID, commit string
	at            time.Time
	values        map[string]objectiveValue
	// declared lists the objectives the task declares as a trade-off, in its order. Empty for a task
	// with one objective.
	declared []string
}

// measurements are the measure results of the run, oldest first (by time, then job), with every rule
// above applied.
func measurements(facts []*cli.TaskFact, run string) []*measured {
	jobs := map[string]*cli.Job{}
	for _, f := range facts {
		if j := f.GetJob(); j != nil && submittedBy(j, run) {
			jobs[j.GetId()] = j
		}
	}
	standing := standingBounds(declaredBounds(facts, run))
	var out []*measured
	counted := map[string]bool{}
	for _, f := range facts {
		r := f.GetResult()
		job, mine := jobs[r.GetJobId()]
		if !mine || r.GetMeasure() == nil || r.GetJobId() == "" || r.GetCommit() == "" || counted[r.GetJobId()] {
			continue
		}
		at, placed := timeOf(r, job)
		if !placed {
			continue
		}
		counted[r.GetJobId()] = true
		values, declared := readings(r.GetMeasure(), standing)
		out = append(out, &measured{jobID: r.GetJobId(), commit: r.GetCommit(), at: at, values: values, declared: declared})
	}
	slices.SortFunc(out, func(a, b *measured) int {
		return cmp.Or(a.at.Compare(b.at), cmp.Compare(a.jobID, b.jobID))
	})
	settleDirections(out)
	markBests(out)
	return out
}

// timeOf is when a result happened: when it was recorded, else when its job finished.
func timeOf(r *cli.Result, job *cli.Job) (time.Time, bool) {
	switch {
	case r.GetRecordedAt() != nil:
		return r.GetRecordedAt().AsTime(), true
	case job.GetFinishedAt() != nil:
		return job.GetFinishedAt().AsTime(), true
	default:
		return time.Time{}, false
	}
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

// readings are what one measure result says of each objective: the scored metric's own headline
// value, and the headline (the mean of the samples) of each observation that is a declared objective
// or has a standing bound. An observation that says no direction takes its bound's. It also returns
// the objectives the result declares as a trade-off, in the task's order. The head's value for a
// bound is read the same way, from the head's result.
func readings(m *cli.MeasureResult, standing map[string]*cli.ObjectiveBound) (values map[string]objectiveValue, declared []string) {
	values = map[string]objectiveValue{}
	says := map[string]cli.Direction{}
	for _, metric := range m.GetObjectives().GetMetrics() {
		says[metric.GetName()] = metric.GetDirection()
		declared = append(declared, metric.GetName())
	}
	if scored := m.GetMetric(); scored.GetName() != "" && scored.GetDirection() != cli.Direction_DIRECTION_UNSPECIFIED && finite(m.GetValue()) {
		values[scored.GetName()] = objectiveValue{value: m.GetValue(), direction: scored.GetDirection()}
	}
	for _, o := range m.GetObservations() {
		_, isDeclared := says[o.GetName()]
		bound := standing[o.GetName()]
		if _, scored := values[o.GetName()]; scored || o.Value == nil || !finite(o.GetValue()) || (!isDeclared && bound == nil) {
			continue
		}
		direction := says[o.GetName()]
		if direction == cli.Direction_DIRECTION_UNSPECIFIED && bound != nil {
			direction = bound.GetDirection()
		}
		if direction != cli.Direction_DIRECTION_UNSPECIFIED {
			values[o.GetName()] = objectiveValue{value: o.GetValue(), direction: direction}
		}
	}
	return values, declared
}

// declaredBounds are the versions of the bounds that stand for the run, by objective, oldest first:
// the run's own and those no run declared (a person at a terminal).
func declaredBounds(facts []*cli.TaskFact, run string) map[string][]*cli.ObjectiveBound {
	out := map[string][]*cli.ObjectiveBound{}
	for _, f := range facts {
		if b := f.GetBound(); b != nil && declaredFor(b, run) {
			out[b.GetObjective()] = append(out[b.GetObjective()], b)
		}
	}
	for _, versions := range out {
		slices.SortStableFunc(versions, func(a, b *cli.ObjectiveBound) int { return cmp.Compare(a.GetSeq(), b.GetSeq()) })
	}
	return out
}

// declaredFor says a bound stands for the run: the run declared it, or no run did (a person did).
func declaredFor(b *cli.ObjectiveBound, run string) bool {
	declared := b.GetOrigin().GetRunId()
	return declared == run || declared == ""
}

// standingBounds are the bounds that stand: the newest version declared for each objective.
func standingBounds(declared map[string][]*cli.ObjectiveBound) map[string]*cli.ObjectiveBound {
	out := make(map[string]*cli.ObjectiveBound, len(declared))
	for objective, versions := range declared {
		out[objective] = versions[len(versions)-1]
	}
	return out
}

// settleDirections drops each value whose direction is not the one its objective's newest
// measurement says, so every objective has one direction.
func settleDirections(all []*measured) {
	newest := map[string]cli.Direction{}
	for _, m := range all {
		for name, v := range m.values {
			newest[name] = v.direction
		}
	}
	for _, m := range all {
		for name, v := range m.values {
			if v.direction != newest[name] {
				delete(m.values, name)
			}
		}
	}
}

// better says a is a better value than b in the direction.
func better(direction cli.Direction, a, b float64) bool {
	if direction == cli.Direction_DIRECTION_MINIMIZE {
		return a < b
	}
	return a > b
}

// markBests sets best on each value that is better than every earlier value of its objective. The
// measurements are in order and share each objective's direction (settleDirections).
func markBests(all []*measured) {
	standing := map[string]float64{}
	for _, m := range all {
		for name, v := range m.values {
			if was, seen := standing[name]; !seen || better(v.direction, v.value, was) {
				standing[name] = v.value
				v.best = true
				m.values[name] = v
			}
		}
	}
}

// directionOf is the way an objective improves, which every value of it shares.
func directionOf(all []*measured, name string) (cli.Direction, bool) {
	for _, m := range slices.Backward(all) {
		if v, ok := m.values[name]; ok {
			return v.direction, true
		}
	}
	return cli.Direction_DIRECTION_UNSPECIFIED, false
}

// objectiveOrder lists the objectives in the order a screen shows them: the newest declared trade-off
// in the order the task declares it, then the others by name.
func objectiveOrder(all []*measured) []string {
	var order []string
	seen := map[string]bool{}
	for _, m := range slices.Backward(all) {
		if len(m.declared) > 0 {
			for _, name := range m.declared {
				if !seen[name] {
					seen[name] = true
					order = append(order, name)
				}
			}
			break
		}
	}
	var rest []string
	for _, m := range all {
		for name := range m.values {
			if !seen[name] {
				seen[name] = true
				rest = append(rest, name)
			}
		}
	}
	slices.Sort(rest)
	return append(order, rest...)
}
