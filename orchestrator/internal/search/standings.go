package search

import (
	"math"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Standings is where a run stands on each of its objectives: its head and its best, with the unit and the
// share of the standing bound each is at, so that a client shows them and reads neither the head, the
// series nor the bounds. They are two things. The head is the candidate the run last promoted, and for a
// task of several objectives it is the best of one of them; the best is the best the run measured on the
// objective, wherever it was measured.
//
// It calculates nothing of its own. The head's measurement is read as every measure result is (readings,
// the one projection of measure results); the bound's head value, best and shares are those Bounds
// already worked out (BestValue is the value bestValues says the series ends at); and for an objective
// with no bound the best is the value of the newest point of its series that the projection marked as
// setting a best, which is the same number read from the other place the projection put it. What is left
// here is their order and shape.
//
// The objectives are those the head's measurement reports, in the order the task declares its metrics
// (just the scored metric for a task of one), then the objectives that have a declared bound and nothing
// measured by the head, by name, then the ones only the series measured, in its order. A head value is the
// bound's own when the run has a bound for the objective in the direction the head measured it, else the
// measurement's, and is absent when the head has none for the objective or it is not a finite number. A
// run with no search state, or whose head measured nothing and which declared nothing, has none.
func Standings(state *pb.SearchState) []*pb.ObjectiveStanding {
	if state == nil {
		return nil
	}
	bounds := make(map[string]*pb.ObjectiveBounds, len(state.GetBounds()))
	standing := make(map[string]*cli.ObjectiveBound, len(state.GetBounds()))
	for _, b := range state.GetBounds() {
		bounds[b.GetObjective()] = b
		if versions := b.GetVersions(); len(versions) > 0 {
			standing[b.GetObjective()] = versions[len(versions)-1]
		}
	}
	type reading struct {
		name  string
		value *float64
	}
	var read []reading
	if m := state.GetHead().GetCurrent().GetResult().GetMeasure(); m != nil {
		values, declared := readings(m, standing)
		names := declared
		if len(names) == 0 {
			names = []string{m.GetMetric().GetName()}
		}
		for _, name := range names {
			var value *float64
			if v, ok := values[name]; ok {
				value = &v.value
			}
			read = append(read, reading{name, value})
		}
	}
	seen := make(map[string]bool, len(read))
	for _, r := range read {
		seen[r.name] = true
	}
	// The bounds come sorted by objective, so the ones the head did not measure follow in name order.
	for _, b := range state.GetBounds() {
		if !seen[b.GetObjective()] {
			seen[b.GetObjective()] = true
			read = append(read, reading{b.GetObjective(), nil})
		}
	}
	series := make(map[string]*pb.ObjectiveSeries, len(state.GetSeries()))
	for _, s := range state.GetSeries() {
		series[s.GetObjective()] = s
		if !seen[s.GetObjective()] {
			seen[s.GetObjective()] = true
			read = append(read, reading{s.GetObjective(), nil})
		}
	}
	out := make([]*pb.ObjectiveStanding, 0, len(read))
	for _, r := range read {
		if r.name == "" {
			continue
		}
		entry := &pb.ObjectiveStanding{Name: r.name, HeadValue: r.value, BestValue: seriesBest(series[r.name])}
		if b := bounds[r.name]; b != nil {
			if b.HeadValue != nil {
				entry.HeadValue = finiteOptional(b.HeadValue)
			}
			entry.HeadPercentOfBound = finiteOptional(b.PercentOfBound)
			if b.BestValue != nil {
				entry.BestValue = finiteOptional(b.BestValue)
			}
			entry.BestPercentOfBound = finiteOptional(b.BestPercentOfBound)
			if versions := b.GetVersions(); len(versions) > 0 {
				entry.Unit = versions[len(versions)-1].GetUnit()
			}
		}
		entry.HeadValue = finiteOptional(entry.HeadValue)
		out = append(out, entry)
	}
	if len(out) > maxBoundObjectives {
		out = out[:maxBoundObjectives]
	}
	return out
}

// seriesBest is the value of the newest point of a series that the projection marked as setting a best.
func seriesBest(s *pb.ObjectiveSeries) *float64 {
	points := s.GetPoints()
	for i := len(points) - 1; i >= 0; i-- {
		if points[i].GetBestSoFar() {
			value := points[i].GetValue()
			return finiteOptional(&value)
		}
	}
	return nil
}

func finiteOptional(v *float64) *float64 {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return nil
	}
	return v
}
