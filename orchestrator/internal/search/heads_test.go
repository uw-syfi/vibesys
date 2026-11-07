package search_test

import (
	"math"
	"slices"
	"testing"
	"testing/quick"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

func measuredHead(measure *cli.MeasureResult) *cli.Head {
	return &cli.Head{State: &cli.Head_Current{Current: &cli.CurrentResult{Result: &cli.Result{Outcome: &cli.Result_Measure{Measure: measure}}}}}
}

func declared(objective string, direction cli.Direction, unit string, head, percent *float64) *pb.ObjectiveBounds {
	return &pb.ObjectiveBounds{
		Objective: objective, Direction: direction, HeadValue: head, PercentOfBound: percent,
		Versions: []*cli.ObjectiveBound{{Objective: objective, Direction: direction, Unit: "old"}, {Objective: objective, Direction: direction, Unit: unit}},
	}
}

func ptr(v float64) *float64 { return &v }

// The head of a run reads as one list of objectives: the ones the head measured in the order the task
// declares its metrics, with the scored metric's value from the measurement and the others' from their
// observations, then the objectives that have a bound and no measurement, each with the standing
// bound's unit and the host's own share of it.
func TestHeadsListTheMeasuredObjectivesThenTheBoundOnesWithTheirUnits(t *testing.T) {
	state := &pb.SearchState{
		Head: measuredHead(&cli.MeasureResult{
			Metric: &cli.Metric{Name: "tput", Direction: cli.Direction_DIRECTION_MAXIMIZE}, Value: 90,
			Observations: []*cli.Observation{{Name: "latency", Value: ptr(12)}, {Name: "memory"}},
			Objectives: &cli.Objectives{Metrics: []*cli.Metric{
				{Name: "latency", Direction: cli.Direction_DIRECTION_MINIMIZE}, {Name: "tput", Direction: cli.Direction_DIRECTION_MAXIMIZE}, {Name: "memory", Direction: cli.Direction_DIRECTION_MINIMIZE},
			}},
		}),
		Bounds: []*pb.ObjectiveBounds{
			declared("accuracy", cli.Direction_DIRECTION_MAXIMIZE, "%", nil, nil),
			declared("tput", cli.Direction_DIRECTION_MAXIMIZE, "tok/s", ptr(90), ptr(45)),
		},
	}
	got := search.Heads(state)
	want := []*pb.ObjectiveHead{
		{Name: "latency", Value: ptr(12)},
		{Name: "tput", Value: ptr(90), Unit: "tok/s", PercentOfBound: ptr(45)},
		{Name: "memory"},
		{Name: "accuracy", Unit: "%"},
	}
	if len(got) != len(want) {
		t.Fatalf("heads %v, want %v", got, want)
	}
	for i := range want {
		if got[i].String() != want[i].String() {
			t.Fatalf("head %d is %v, want %v", i, got[i], want[i])
		}
	}
	if search.Heads(nil) != nil || len(search.Heads(&pb.SearchState{})) != 0 {
		t.Fatal("a run with nothing measured and no bound has no heads")
	}
}

// A task of one objective scores one metric, whatever the head's observations hold, and a value that is
// not a finite number is not served, since a client would draw it.
func TestAHeadValueThatIsNotANumberIsNotServed(t *testing.T) {
	for _, v := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		got := search.Heads(&pb.SearchState{Head: measuredHead(&cli.MeasureResult{
			Metric: &cli.Metric{Name: "tput"}, Value: v, Observations: []*cli.Observation{{Name: "other", Value: ptr(1)}},
		})})
		if len(got) != 1 || got[0].Name != "tput" || got[0].Value != nil {
			t.Fatalf("a head of %v serves %v", v, got)
		}
	}
}

// Whatever the head and the bounds hold, the heads name each objective once, never serve an unnamed one
// or a value that is not finite, stay within the contract's cap, and put the objectives with a
// measurement before those without.
func TestHeadsNameEachObjectiveOnceWithAFiniteValue(t *testing.T) {
	prop := func(measured, bounded []uint8, values []float64) bool {
		names := func(ids []uint8) []string {
			var out []string
			for _, id := range ids {
				out = append(out, string(rune('a'+id%12)))
			}
			return out
		}
		value := func(i int) float64 {
			if len(values) == 0 {
				return 1
			}
			return values[i%len(values)]
		}
		m := &cli.MeasureResult{Metric: &cli.Metric{Name: "a"}, Value: value(0)}
		var metrics []*cli.Metric
		declaredOnce := map[string]bool{}
		for i, name := range names(measured) {
			// A task declares each objective once.
			if declaredOnce[name] {
				continue
			}
			declaredOnce[name] = true
			m.Observations = append(m.Observations, &cli.Observation{Name: name, Value: ptr(value(i + 1))})
			metrics = append(metrics, &cli.Metric{Name: name})
		}
		if len(metrics) > 0 {
			m.Objectives = &cli.Objectives{Metrics: metrics}
		}
		state := &pb.SearchState{Head: measuredHead(m)}
		seen := map[string]bool{}
		for _, name := range names(bounded) {
			if !seen[name] {
				seen[name] = true
				state.Bounds = append(state.Bounds, declared(name, cli.Direction_DIRECTION_MAXIMIZE, "u", ptr(value(len(name))), nil))
			}
		}
		slices.SortFunc(state.Bounds, func(a, b *pb.ObjectiveBounds) int { return compareStrings(a.Objective, b.Objective) })
		got := search.Heads(state)
		once := map[string]bool{}
		for _, h := range got {
			if h.Name == "" || once[h.Name] || (h.Value != nil && (math.IsNaN(*h.Value) || math.IsInf(*h.Value, 0))) {
				return false
			}
			once[h.Name] = true
		}
		return len(got) <= 64
	}
	if err := quick.Check(prop, nil); err != nil {
		t.Fatal(err)
	}
}

func compareStrings(a, b string) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}
