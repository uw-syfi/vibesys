package search

import (
	"cmp"
	"math"
	"slices"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// The limits of what the view carries of the measured history, which are the limits the contract
// states for it.
const (
	maxSeriesObjectives   = 8
	maxSeriesPoints       = 400
	maxTradeoffObjectives = 8
	maxTradeoffPoints     = 400
)

// objectiveValue is one candidate's headline number on one objective, with the way the measurement
// said the objective improves.
type objectiveValue struct {
	value     float64
	direction cli.Direction
}

// measured is one measure result of the run: the candidate, when it was recorded, and its value on
// each objective it reports.
type measured struct {
	jobID, commit string
	at            *timestamppb.Timestamp
	values        map[string]objectiveValue
	// declared lists the objectives the task declares as a trade-off, in its order. Empty for a task
	// with one objective.
	declared []string
}

// Trajectory is the measured history of one run, derived from the facts of its measure jobs: one
// series per objective with the points that set a new best marked, and, when the task declares two or
// more objectives, every measured candidate on all of them with the ones no other candidate beats
// marked. It is the only place these are worked out, so a screen draws them and computes nothing.
//
// An objective is the metric a measurement scores, an objective its task declares as a trade-off, or
// another observation the run declared a bound for: the other readings are auxiliary and are not
// charted. An observation that does not say which way it improves takes its bound's direction, and
// one with neither is left out. A measurement that says the other way than the newest one does for an
// objective (the task changed) is left out of that objective.
//
// A measurement is a measure result of a job this run submitted: the store holds the results of every
// run of the task, and another run's say nothing of this one. The result says whether anything was
// left out: more objectives than fit, and more points than fit (the oldest go, except that a
// trade-off keeps its frontier before its newer candidates).
func Trajectory(facts []*cli.TaskFact, run string) (series []*pb.ObjectiveSeries, tradeoff *pb.Tradeoff, truncated bool) {
	all := measurements(facts, run)
	if len(all) == 0 {
		return nil, nil, false
	}
	series, truncated = seriesOf(all)
	tradeoff, left := tradeoffOf(all)
	return series, tradeoff, truncated || left
}

// measurements are the run's measure results, oldest first.
func measurements(facts []*cli.TaskFact, run string) []*measured {
	jobs := map[string]bool{}
	standing := map[string]*cli.ObjectiveBound{}
	for _, f := range facts {
		if j := f.GetJob(); j != nil && submittedBy(j, run) {
			jobs[j.GetId()] = true
		}
		if b := f.GetBound(); b != nil && b.GetOrigin().GetRunId() == run {
			if old := standing[b.GetObjective()]; old == nil || b.GetSeq() > old.GetSeq() {
				standing[b.GetObjective()] = b
			}
		}
	}
	var out []*measured
	for _, f := range facts {
		r := f.GetResult()
		if r == nil || r.GetMeasure() == nil || !jobs[r.GetJobId()] || r.GetRecordedAt() == nil || r.GetJobId() == "" || r.GetCommit() == "" {
			continue
		}
		out = append(out, measurementOf(r, standing))
	}
	slices.SortFunc(out, func(a, b *measured) int {
		if c := a.at.AsTime().Compare(b.at.AsTime()); c != 0 {
			return c
		}
		return cmp.Compare(a.jobID, b.jobID)
	})
	return out
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

// measurementOf reads a result's objectives: the scored metric, the declared trade-off objectives
// and the observations the run declared a bound for.
func measurementOf(r *cli.Result, bounds map[string]*cli.ObjectiveBound) *measured {
	m := r.GetMeasure()
	out := &measured{jobID: r.GetJobId(), commit: r.GetCommit(), at: r.GetRecordedAt(), values: map[string]objectiveValue{}}
	says := map[string]cli.Direction{}
	for _, metric := range m.GetObjectives().GetMetrics() {
		says[metric.GetName()] = metric.GetDirection()
		out.declared = append(out.declared, metric.GetName())
	}
	if scored := m.GetMetric(); scored.GetName() != "" && scored.GetDirection() != cli.Direction_DIRECTION_UNSPECIFIED && finite(m.GetValue()) {
		out.values[scored.GetName()] = objectiveValue{m.GetValue(), scored.GetDirection()}
	}
	for _, o := range m.GetObservations() {
		_, declared := says[o.GetName()]
		bound := bounds[o.GetName()]
		if o.Value == nil || !finite(o.GetValue()) || (!declared && bound == nil) {
			continue
		}
		direction := says[o.GetName()]
		if direction == cli.Direction_DIRECTION_UNSPECIFIED && bound != nil {
			direction = bound.GetDirection()
		}
		if direction != cli.Direction_DIRECTION_UNSPECIFIED {
			out.values[o.GetName()] = objectiveValue{o.GetValue(), direction}
		}
	}
	return out
}

// better says a is a better value than b in the direction.
func better(direction cli.Direction, a, b float64) bool {
	if direction == cli.Direction_DIRECTION_MINIMIZE {
		return a < b
	}
	return a > b
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

// directionOf is the way an objective improves as its newest measurement says it.
func directionOf(all []*measured, name string) (cli.Direction, bool) {
	for _, m := range slices.Backward(all) {
		if v, ok := m.values[name]; ok {
			return v.direction, true
		}
	}
	return cli.Direction_DIRECTION_UNSPECIFIED, false
}

func seriesOf(all []*measured) (out []*pb.ObjectiveSeries, truncated bool) {
	for _, name := range objectiveOrder(all) {
		direction, ok := directionOf(all, name)
		if !ok {
			continue
		}
		s := &pb.ObjectiveSeries{Objective: name, Direction: direction}
		var best float64
		for _, m := range all {
			v, ok := m.values[name]
			if !ok || v.direction != direction {
				continue
			}
			isBest := len(s.Points) == 0 || better(direction, v.value, best)
			if isBest {
				best = v.value
			}
			s.Points = append(s.Points, &pb.MeasuredPoint{JobId: m.jobID, Commit: m.commit, At: m.at, Value: v.value, BestSoFar: isBest})
		}
		if len(s.Points) == 0 {
			continue
		}
		if len(out) == maxSeriesObjectives {
			truncated = true
			break
		}
		if len(s.Points) > maxSeriesPoints {
			s.Points, truncated = s.Points[len(s.Points)-maxSeriesPoints:], true
		}
		out = append(out, s)
	}
	return out, truncated
}

// tradeoffOf places every candidate measured on all of the newest declared objectives on them at
// once. It is absent when no measurement declares two objectives.
func tradeoffOf(all []*measured) (*pb.Tradeoff, bool) {
	var names []string
	for _, m := range slices.Backward(all) {
		if len(m.declared) >= 2 {
			names = m.declared
			break
		}
	}
	if len(names) < 2 {
		return nil, false
	}
	truncated := len(names) > maxTradeoffObjectives
	names = names[:min(len(names), maxTradeoffObjectives)]
	out := &pb.Tradeoff{}
	var directions []cli.Direction
	for _, name := range names {
		direction, ok := directionOf(all, name)
		if !ok {
			return nil, false
		}
		directions = append(directions, direction)
		out.Objectives = append(out.Objectives, &pb.TradeoffObjective{Name: name, Direction: direction})
	}
	var rows []*pb.TradeoffPoint
candidates:
	for _, m := range all {
		values := make([]float64, len(names))
		for i, name := range names {
			v, ok := m.values[name]
			if !ok || v.direction != directions[i] {
				continue candidates
			}
			values[i] = v.value
		}
		rows = append(rows, &pb.TradeoffPoint{JobId: m.jobID, Commit: m.commit, At: m.at, Values: values})
	}
	markFrontier(rows, directions)
	out.Points, truncated = keepFrontierFirst(rows, maxTradeoffPoints), truncated || len(rows) > maxTradeoffPoints
	return out, truncated
}

// dominates says a is at least as good as b on every objective and better on one.
func dominates(directions []cli.Direction, a, b []float64) bool {
	strictly := false
	for i, d := range directions {
		if better(d, b[i], a[i]) {
			return false
		}
		strictly = strictly || better(d, a[i], b[i])
	}
	return strictly
}

// markFrontier sets on_frontier on the rows no other row dominates.
func markFrontier(rows []*pb.TradeoffPoint, directions []cli.Direction) {
	for _, row := range rows {
		row.OnFrontier = !slices.ContainsFunc(rows, func(other *pb.TradeoffPoint) bool {
			return other != row && dominates(directions, other.Values, row.Values)
		})
	}
}

// keepFrontierFirst keeps at most n rows, in their order: the newest frontier rows, then the newest
// of the others with the room that is left, so that what the screen draws as the frontier is what no
// candidate beats whatever the run's length.
func keepFrontierFirst(rows []*pb.TradeoffPoint, n int) []*pb.TradeoffPoint {
	if len(rows) <= n {
		return rows
	}
	keep := make([]bool, len(rows))
	room := n
	for _, onFrontier := range []bool{true, false} {
		for i := len(rows) - 1; i >= 0 && room > 0; i-- {
			if rows[i].OnFrontier == onFrontier {
				keep[i], room = true, room-1
			}
		}
	}
	var out []*pb.TradeoffPoint
	for i, row := range rows {
		if keep[i] {
			out = append(out, row)
		}
	}
	return out
}

// shrinkTrajectory drops the oldest points of the series and the trade-off until the state fits the
// byte budget, which only a run with many objectives and thousands of measurements can need: the
// history is a few small points per measurement, so it goes after every raw row.
func shrinkTrajectory(s *pb.SearchState, fits func() bool) {
	for !fits() && (len(s.Series) > 0 || len(s.GetTradeoff().GetPoints()) > 0) {
		for _, series := range s.Series {
			series.Points = series.Points[len(series.Points)/4+1:]
		}
		s.Series = slices.DeleteFunc(s.Series, func(series *pb.ObjectiveSeries) bool { return len(series.Points) == 0 })
		if t := s.GetTradeoff(); t != nil {
			t.Points = t.Points[min(len(t.Points), len(t.Points)/4+1):]
		}
	}
}
