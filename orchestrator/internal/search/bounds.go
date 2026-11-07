package search

import (
	"cmp"
	"math"
	"slices"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// The limits of what the view carries of the declared bounds, which are the limits the contract
// states for it.
const (
	maxBoundObjectives = 64
	maxBoundVersions   = 64
)

// Bounds is the view of the performance bounds the agents of one run declared: one entry per
// objective, by name, with every version of the bound oldest first, so a chart can draw the bound as
// it stood at each moment, and with how close the head, and the best the run measured, are to the
// bound that stands. The best is the one the series ends at (measurements): the head is the candidate
// the run last promoted, which on an objective other than the one it was promoted for need not be it.
//
// The bounds shown are the run's own and those a person declared outside any run: the store holds
// those of every run of the task, as it holds their jobs, and a bound declared in an earlier run says
// nothing of this one. A declaration with no run (RecordOrigin.run_id empty, a person at a terminal)
// is the person's statement of what the task can reach, not an agent's derivation for one run, so it
// stands for every run of the task: it is a version like any other, and it is the standing one when it
// is the newest declared for its objective. The version's origin says who declared it. An empty run
// id is matched by declaredFor and nowhere else.
//
// The result says whether anything was left out: an objective past the limit (the ones whose newest
// bound is oldest go), versions past the limit (the oldest go), and versions of another direction than
// the newest one's, which no longer say anything about the objective as the task has it now.
func Bounds(facts []*cli.TaskFact, run string, head *cli.Head) (out []*pb.ObjectiveBounds, truncated bool) {
	declared := declaredBounds(facts, run)
	heads := headReadings(head, standingBounds(declared))
	bests := bestValues(measurements(facts, run))
	for objective, versions := range declared {
		standing := versions[len(versions)-1]
		kept := slices.DeleteFunc(slices.Clone(versions), func(b *cli.ObjectiveBound) bool { return b.GetDirection() != standing.GetDirection() })
		truncated = truncated || len(kept) != len(versions) || len(kept) > maxBoundVersions
		if len(kept) > maxBoundVersions {
			kept = kept[len(kept)-maxBoundVersions:]
		}
		entry := &pb.ObjectiveBounds{Objective: objective, Direction: standing.GetDirection()}
		for _, b := range kept {
			entry.Versions = append(entry.Versions, cloneBound(b))
		}
		if reading, ok := heads[objective]; ok && reading.direction == standing.GetDirection() {
			value := reading.value
			entry.HeadValue = &value
			if percent, ok := PercentOfBound(standing.GetDirection(), standing.GetValue(), value); ok {
				entry.PercentOfBound = &percent
			}
		}
		if best, ok := bests[objective]; ok && best.direction == standing.GetDirection() {
			value := best.value
			entry.BestValue = &value
			if percent, ok := PercentOfBound(standing.GetDirection(), standing.GetValue(), value); ok {
				entry.BestPercentOfBound = &percent
			}
		}
		out = append(out, entry)
	}
	if len(out) > maxBoundObjectives {
		// Keep the objectives with the newest standing bound.
		slices.SortStableFunc(out, func(a, b *pb.ObjectiveBounds) int {
			return -cmp.Compare(standingSeq(a), standingSeq(b))
		})
		out, truncated = out[:maxBoundObjectives], true
	}
	slices.SortFunc(out, func(a, b *pb.ObjectiveBounds) int { return cmp.Compare(a.Objective, b.Objective) })
	return out, truncated
}

func standingSeq(b *pb.ObjectiveBounds) uint64 { return b.Versions[len(b.Versions)-1].GetSeq() }

func cloneBound(b *cli.ObjectiveBound) *cli.ObjectiveBound {
	return &cli.ObjectiveBound{
		Seq: b.GetSeq(), Origin: &cli.RecordOrigin{RunId: b.GetOrigin().GetRunId(), Agent: b.GetOrigin().GetAgent(), At: b.GetOrigin().GetAt()},
		Objective: b.GetObjective(), Direction: b.GetDirection(), Value: b.GetValue(), Unit: b.GetUnit(), Note: b.GetNote(),
	}
}

// headReadings are the head's value on each objective, the numbers a percent of a bound is a share
// of: the measurement's value for the metric it scores, and the mean of the samples of another
// objective, which the daemon computes. They are read as any measure result is (readings), so the
// head's number for an objective is the number the measured history holds for the same result. There
// are none when the run has no head. The head's direction is for the caller to compare with the
// bound's, since a share of a bound in the other direction would mean nothing.
func headReadings(head *cli.Head, standing map[string]*cli.ObjectiveBound) map[string]objectiveValue {
	m := head.GetCurrent().GetResult().GetMeasure()
	if m == nil {
		return nil
	}
	values, _ := readings(m, standing)
	return values
}

// PercentOfBound is how close a value is to the bound of its objective, in percent: 100 at the
// bound, and more past it. For an objective that is maximized it is the value over the bound, and for
// one that is minimized the bound over the value, so more is better for both. It is not defined, and
// says so, when the direction is not one, when either number is not finite and above zero, and when
// the share does not fit a float.
func PercentOfBound(direction cli.Direction, bound, value float64) (float64, bool) {
	if !(bound > 0) || !(value > 0) || math.IsInf(bound, 0) || math.IsInf(value, 0) {
		return 0, false
	}
	var percent float64
	switch direction {
	case cli.Direction_DIRECTION_MAXIMIZE:
		percent = value / bound * 100
	case cli.Direction_DIRECTION_MINIMIZE:
		percent = bound / value * 100
	default:
		return 0, false
	}
	if math.IsInf(percent, 0) || math.IsNaN(percent) || percent <= 0 {
		return 0, false
	}
	return percent, true
}
