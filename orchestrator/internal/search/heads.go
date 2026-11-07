package search

import (
	"math"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Heads is the head's value on each objective of a run, with the unit and the share of the standing
// bound the agents declared, so that a client shows them and reads neither the head nor the bounds.
//
// The objectives are those the head's measurement reports, in the order the task declares its metrics
// (just the scored metric for a task of one), then the objectives that have a declared bound and no
// head value, by name. A value is the bound's own head value when the run has a bound for the objective
// (which says the head measured it in the bound's direction), else the measurement's, and is absent
// when the head has none for the objective or it is not a finite number. A run with no search state, or
// whose head measured nothing and which declared no bound, has none.
func Heads(state *pb.SearchState) []*pb.ObjectiveHead {
	if state == nil {
		return nil
	}
	bounds := make(map[string]*pb.ObjectiveBounds, len(state.GetBounds()))
	for _, b := range state.GetBounds() {
		bounds[b.GetObjective()] = b
	}
	type reading struct {
		name  string
		value *float64
	}
	var read []reading
	if m := state.GetHead().GetCurrent().GetResult().GetMeasure(); m != nil {
		scored := m.GetMetric().GetName()
		names := []string{scored}
		if metrics := m.GetObjectives().GetMetrics(); len(metrics) > 0 {
			names = names[:0]
			for _, metric := range metrics {
				names = append(names, metric.GetName())
			}
		}
		for _, name := range names {
			var value *float64
			if name == scored {
				value = finiteValue(m.GetValue())
			} else {
				for _, o := range m.GetObservations() {
					if o.GetName() == name {
						value = finiteOptional(o.Value)
						break
					}
				}
			}
			read = append(read, reading{name, value})
		}
	}
	seen := make(map[string]bool, len(read))
	for _, r := range read {
		seen[r.name] = true
	}
	// The bounds come sorted by objective, so the ones with no measurement follow in name order.
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

func finiteValue(v float64) *float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return nil
	}
	return &v
}

func finiteOptional(v *float64) *float64 {
	if v == nil {
		return nil
	}
	return finiteValue(*v)
}
