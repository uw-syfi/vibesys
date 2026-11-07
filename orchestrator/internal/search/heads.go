package search

import (
	"math"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Heads is the head's value on each objective of a run, with the unit and the share of the standing
// bound the agents declared, so that a client shows them and reads neither the head nor the bounds. It
// is the head's and not the best the run measured: the head is the candidate the run last promoted, and
// the best is on the objective's bounds (ObjectiveBounds.best_value).
//
// It calculates nothing of its own. The head's measurement is read as every measure result is
// (readings, the one projection of measure results), the bound's head value and share are those Bounds
// already worked out, and what is left here is their order and shape: the objectives the head's
// measurement reports, in the order the task declares its metrics (just the scored metric for a task
// of one), then the objectives that have a declared bound and nothing measured by the head, by name. A
// value is the bound's own head value when the run has a bound for the objective in the direction the
// head measured it, else the measurement's, and is absent when the head has none for the objective or it
// is not a finite number. A run with no search state, or whose head measured nothing and which declared
// no bound, has none.
func Heads(state *pb.SearchState) []*pb.ObjectiveHead {
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
			read = append(read, reading{b.GetObjective(), nil})
		}
	}
	out := make([]*pb.ObjectiveHead, 0, len(read))
	for _, r := range read {
		if r.name == "" {
			continue
		}
		head := &pb.ObjectiveHead{Name: r.name, Value: r.value}
		if b := bounds[r.name]; b != nil {
			if b.HeadValue != nil {
				head.Value = finiteOptional(b.HeadValue)
			}
			head.PercentOfBound = finiteOptional(b.PercentOfBound)
			if versions := b.GetVersions(); len(versions) > 0 {
				head.Unit = versions[len(versions)-1].GetUnit()
			}
		}
		out = append(out, head)
	}
	if len(out) > maxBoundObjectives {
		out = out[:maxBoundObjectives]
	}
	return out
}

func finiteOptional(v *float64) *float64 {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return nil
	}
	return v
}
