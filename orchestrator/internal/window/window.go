// Package window works out what happened in a run between two times: the facts a check-in freezes
// when it is made. It is pure. It reads no clock and does no I/O, so the facts are a function of the
// records it is given and the window, whatever order the records come in and however often a stream
// repeated one.
//
// The records are the host's own: the uncapped view of the task's job log (search.View, which the
// wire shape caps and this does not), the ledger of the run's finished turns (cost.Ledger), the run's
// agents and the run's messages. A window is the half-open span [Since, Until), so a record dated
// exactly Until is in the next window and consecutive windows share none.
//
// The facts are additive where a count or a sum can be: over [a, c] they equal the facts over [a, b]
// plus those over [b, c] for the spend, the agents, the measurements, and the sets of plans and reports
// (as unions). The best value of an objective is not a sum; it is chained instead, the end of one
// window being the start of the next.
//
// A fact the records do not give is absent and never zero: a price no turn reported, an objective
// nothing measured, a direction the job log did not name (such an objective is left out, because "best"
// would be a guess).
package window

import (
	"cmp"
	"maps"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// Input is the records Facts reads. Nothing in it is changed.
type Input struct {
	Run core.RunID
	// Since and Until bound the window [Since, Until).
	Since, Until time.Time
	// View is the task's whole job log as the host last read it. It holds the jobs of every run of the
	// task; only the run's are counted.
	View search.View
	// Ledger holds the run's finished turns. Nil holds none.
	Ledger *cost.Ledger
	// Agents are the run's agents, and Messages the run's messages, of which the reports are counted.
	Agents   []core.Agent
	Messages []core.Message
}

// Facts works out what happened in the window.
func Facts(in Input) core.WindowFacts {
	w := span{from: in.Since, to: in.Until}
	var out core.WindowFacts
	if in.Ledger != nil {
		out.Spend = in.Ledger.Spend(in.Since, in.Until)
	}
	for _, a := range in.Agents {
		if a.Run != in.Run {
			continue
		}
		if w.holds(a.StartedAt) {
			out.AgentsStarted++
		}
		if !a.EndedAt.IsZero() && w.holds(a.EndedAt) {
			out.AgentsEnded++
		}
	}
	out.Reports = reports(in, w)
	measured := in.View.Measurements(in.Run)
	for _, m := range measured {
		if w.holds(m.At) {
			out.JobsMeasured++
		}
	}
	out.Objectives = objectives(measured, w)
	out.PlansCreated, out.PlansChanged = plans(in.View.Plans(in.Run), w)
	return out
}

// span is a window.
type span struct{ from, to time.Time }

// holds says a time is in [from, to).
func (s span) holds(t time.Time) bool { return !t.Before(s.from) && t.Before(s.to) }

// reports are the reports the lead posted in the window, in the order posted.
func reports(in Input, w span) []core.MessageID {
	var found []core.Message
	for _, m := range in.Messages {
		if _, ok := m.Body.(core.Report); ok && m.Run == in.Run && w.holds(m.At) {
			found = append(found, m)
		}
	}
	slices.SortFunc(found, func(a, b core.Message) int { return cmp.Or(a.At.Compare(b.At), cmp.Compare(a.ID, b.ID)) })
	var out []core.MessageID
	for _, m := range found {
		if len(out) == 0 || out[len(out)-1] != m.ID {
			out = append(out, m.ID)
		}
	}
	return out
}

// point is one objective's value in one measurement, and whether the measurement set the best so far
// of the objective: the mark is the measurements' own (search.Value.Best), so the window works out
// no best of its own.
type point struct {
	at        time.Time
	direction core.Direction
	unit      string
	value     float64
	job       core.JobID
	commit    core.Commit
	best      bool
}

// objectives says, for each objective measured by the end of the window, its best value before the
// window and its best value before its end.
func objectives(measured []search.Measurement, w span) []core.ObjectiveMove {
	byMetric := map[string][]point{}
	for _, m := range measured {
		for _, v := range m.Values {
			byMetric[v.Metric] = append(byMetric[v.Metric], point{at: m.At, direction: v.Direction, unit: v.Unit, value: v.Number, job: m.Job, commit: m.Commit, best: v.Best})
		}
	}
	var out []core.ObjectiveMove
	for _, metric := range slices.Sorted(maps.Keys(byMetric)) {
		ps := byMetric[metric] // in order of date, as the measurements are
		units := make([]string, len(ps))
		for i, p := range ps {
			units[i] = p.unit
		}
		move := core.ObjectiveMove{Metric: metric, Direction: ps[0].direction, Unit: search.CommonUnit(units)}
		move.Start, move.End = bestBefore(ps, w.from), bestBefore(ps, w.to)
		if move.End != nil {
			out = append(out, move)
		}
	}
	return out
}

// bestBefore is where the best stood before t: the newest point dated before t that set a best, among
// the points, which are in order of date. It is nil when none is.
func bestBefore(ps []point, t time.Time) *core.Best {
	var found *point
	for i := range ps {
		p := &ps[i]
		if !p.at.Before(t) {
			break
		}
		if p.best {
			found = p
		}
	}
	if found == nil {
		return nil
	}
	return &core.Best{Value: found.value, Job: found.job, Commit: found.commit, At: found.at}
}

// plans are the plans the run made in the window, and the others whose status the run set in it, by
// number. Which plans are the run's is the view's (search.View.Plans, the rule the run's ideas use),
// never inferred from when a plan was made or edited: another run's plans, and a person's, are not
// counted. A plan the run made in the window is only made, whatever else it set; a plan made by
// another run, or earlier, is changed when the run set one of its statuses in the window.
func plans(all []search.Plan, w span) (created, changed []uint32) {
	for _, p := range all {
		switch {
		case p.MadeByRun && !p.CreatedAt.IsZero() && w.holds(p.CreatedAt):
			created = append(created, p.Number)
		case slices.ContainsFunc(p.RunTransitions, w.holds):
			changed = append(changed, p.Number)
		}
	}
	slices.Sort(created)
	slices.Sort(changed)
	return created, changed
}
