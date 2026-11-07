package search

import (
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

// Trajectory is the measured history of one run, derived from the one projection of its measure
// results (measurements): one series per objective with the points that set a new best marked, and,
// when the task declares two or more objectives, every measured candidate on all of them with the
// ones no other candidate beats marked. It is the only place these are worked out, so a screen draws
// them and computes nothing. Which results count, the objectives, their direction and the best so far
// are the projection's, shared with the window facts of a check-in and the head's share of a bound.
//
// The result says whether anything was left out: more objectives than fit, and more points than fit.
// A series keeps the points that set a best before the others, and a trade-off keeps its frontier
// before its other candidates; the oldest of each go first. So a cut series always holds the best
// the run reached, and the newest points, and a cut trade-off holds its frontier.
func Trajectory(facts []*cli.TaskFact, run string) (series []*pb.ObjectiveSeries, tradeoff *pb.Tradeoff, truncated bool) {
	all := measurements(facts, run)
	if len(all) == 0 {
		return nil, nil, false
	}
	series, truncated = seriesOf(all)
	tradeoff, left := tradeoffOf(all)
	return series, tradeoff, truncated || left
}

func seriesOf(all []*measured) (out []*pb.ObjectiveSeries, truncated bool) {
	for _, name := range objectiveOrder(all) {
		direction, ok := directionOf(all, name)
		if !ok {
			continue
		}
		var points []*pb.MeasuredPoint
		var units []string
		for _, m := range all {
			if v, ok := m.values[name]; ok {
				units = append(units, v.unit)
				points = append(points, &pb.MeasuredPoint{JobId: m.jobID, Commit: m.commit, At: timestamppb.New(m.at), Value: v.value, BestSoFar: v.best})
			}
		}
		if len(points) == 0 {
			continue
		}
		if len(out) == maxSeriesObjectives {
			truncated = true
			break
		}
		kept := keepFirst(points, maxSeriesPoints, func(p *pb.MeasuredPoint) bool { return p.BestSoFar })
		truncated = truncated || len(kept) < len(points)
		out = append(out, &pb.ObjectiveSeries{Objective: name, Direction: direction, Points: kept, Unit: CommonUnit(units)})
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
			if !ok {
				continue candidates
			}
			values[i] = v.value
		}
		rows = append(rows, &pb.TradeoffPoint{JobId: m.jobID, Commit: m.commit, At: timestamppb.New(m.at), Values: values})
	}
	markFrontier(rows, directions)
	out.Points = keepFirst(rows, maxTradeoffPoints, func(p *pb.TradeoffPoint) bool { return p.OnFrontier })
	return out, truncated || len(out.Points) < len(rows)
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

// keepFirst keeps at most n rows, in their order: the newest rows that matter (a point that set a
// best, a candidate on the frontier), then the newest of the others with the room that is left. What
// a screen draws of what matters is then what the run reached, whatever the run's length.
func keepFirst[T any](rows []T, n int, matters func(T) bool) []T {
	if len(rows) <= n {
		return rows
	}
	keep := make([]bool, len(rows))
	room := n
	for _, mattering := range []bool{true, false} {
		for i := len(rows) - 1; i >= 0 && room > 0; i-- {
			if matters(rows[i]) == mattering {
				keep[i], room = true, room-1
			}
		}
	}
	var out []T
	for i, row := range rows {
		if keep[i] {
			out = append(out, row)
		}
	}
	return out
}

// shrinkTrajectory drops points of the series and the trade-off until the state fits the byte budget,
// which only a run with many objectives and thousands of measurements can need: the history is a few
// small points per measurement, so it goes after every raw row. What goes first is what matters least:
// the oldest points that set no best and the oldest candidates off the frontier, together, for as long
// as there are any. Only then do the older points that set a best go (a series keeps its newest, which
// is its best, so it is never empty) and the older frontier candidates, so a frontier candidate never
// goes while another remains, and a series never loses the best the run reached before it loses all
// its other points.
func shrinkTrajectory(s *pb.SearchState, fits func() bool) {
	for !fits() && shrinkOnce(s) {
	}
}

// shrinkOnce drops one batch from the least that matters, and says whether it dropped anything.
func shrinkOnce(s *pb.SearchState) bool {
	before := pointsIn(s)
	for _, series := range s.Series {
		series.Points = dropOldest(series.Points, func(p *pb.MeasuredPoint) bool { return !p.BestSoFar })
	}
	if t := s.GetTradeoff(); t != nil {
		t.Points = dropOldest(t.Points, func(p *pb.TradeoffPoint) bool { return !p.OnFrontier })
	}
	if pointsIn(s) < before {
		return true
	}
	for _, series := range s.Series {
		newest := series.Points[len(series.Points)-1]
		series.Points = dropOldest(series.Points, func(p *pb.MeasuredPoint) bool { return p != newest })
	}
	if t := s.GetTradeoff(); t != nil {
		t.Points = dropOldest(t.Points, func(*pb.TradeoffPoint) bool { return true })
	}
	return pointsIn(s) < before
}

func pointsIn(s *pb.SearchState) int {
	n := len(s.GetTradeoff().GetPoints())
	for _, series := range s.Series {
		n += len(series.Points)
	}
	return n
}

// dropOldest removes the oldest quarter, and one more, of the rows that may go. The others stay in
// their order.
func dropOldest[T any](rows []T, mayGo func(T) bool) []T {
	n := 0
	for _, row := range rows {
		if mayGo(row) {
			n++
		}
	}
	drop := n/4 + min(n, 1)
	var out []T
	for _, row := range rows {
		if drop > 0 && mayGo(row) {
			drop--
			continue
		}
		out = append(out, row)
	}
	return out
}
