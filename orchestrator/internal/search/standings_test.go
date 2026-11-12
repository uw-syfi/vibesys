package search_test

import (
	"math"
	"slices"
	"testing"
	"testing/quick"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/propcheck"
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

func seriesOf(objective string, values ...float64) *pb.ObjectiveSeries {
	s := &pb.ObjectiveSeries{Objective: objective, Direction: cli.Direction_DIRECTION_MAXIMIZE}
	best := math.Inf(-1)
	for _, v := range values {
		s.Points = append(s.Points, &pb.MeasuredPoint{Value: v, BestSoFar: v > best})
		best = math.Max(best, v)
	}
	return s
}

func sameStandings(t *testing.T, got, want []*pb.ObjectiveStanding) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("standings %v, want %v", got, want)
	}
	for i := range want {
		if got[i].String() != want[i].String() {
			t.Fatalf("standing %d is %v, want %v", i, got[i], want[i])
		}
	}
}

// Where a run stands reads as one list of objectives: the ones the head measured in the order the task
// declares its metrics, with the scored metric's value from the measurement and the others' from their
// observations, then the objectives that have a bound and no measurement by the head, then the ones only
// the series measured, each with the standing bound's unit and the host's own share of it.
func TestStandingsListTheMeasuredObjectivesThenTheBoundOnesThenTheSeriesOnes(t *testing.T) {
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
		Series: []*pb.ObjectiveSeries{seriesOf("score", 3, 5)},
	}
	sameStandings(t, search.Standings(state), []*pb.ObjectiveStanding{
		{Name: "latency", HeadValue: ptr(12)},
		{Name: "tput", HeadValue: ptr(90), BoundUnit: "tok/s", HeadPercentOfBound: ptr(45)},
		{Name: "memory"},
		{Name: "accuracy", BoundUnit: "%"},
		{Name: "score", BestValue: ptr(5)},
	})
	if search.Standings(nil) != nil || len(search.Standings(&pb.SearchState{})) != 0 {
		t.Fatal("a run with nothing measured and no bound has no standings")
	}
}

// The head and the best are two things. The head is the candidate the run last promoted, which on an
// objective it was not promoted for need not be the best the run measured on it: each is served as it is,
// the best from the bound when the run has one (with its share) and from the series when it has none,
// and neither is used for the other.
func TestTheHeadAndTheBestAreServedApartAndNeitherStandsInForTheOther(t *testing.T) {
	bounded := declared("latency", cli.Direction_DIRECTION_MINIMIZE, "ms", ptr(30), ptr(50))
	bounded.BestValue, bounded.BestPercentOfBound = ptr(12), ptr(125)
	state := &pb.SearchState{
		Head: measuredHead(&cli.MeasureResult{
			Metric: &cli.Metric{Name: "tput", Direction: cli.Direction_DIRECTION_MAXIMIZE}, Value: 60,
			Observations: []*cli.Observation{{Name: "latency", Value: ptr(30)}},
			Objectives: &cli.Objectives{Metrics: []*cli.Metric{
				{Name: "tput", Direction: cli.Direction_DIRECTION_MAXIMIZE}, {Name: "latency", Direction: cli.Direction_DIRECTION_MINIMIZE},
			}},
		}),
		Bounds: []*pb.ObjectiveBounds{bounded},
		Series: []*pb.ObjectiveSeries{seriesOf("tput", 40, 90, 70, 60)},
	}
	sameStandings(t, search.Standings(state), []*pb.ObjectiveStanding{
		// No bound: the head is the last measurement the run promoted, the best is where the series' best line ends.
		{Name: "tput", HeadValue: ptr(60), BestValue: ptr(90)},
		// A bound: the host's own best and its share of the bound.
		{Name: "latency", HeadValue: ptr(30), BoundUnit: "ms", HeadPercentOfBound: ptr(50), BestValue: ptr(12), BestPercentOfBound: ptr(125)},
	})
}

// A value that is not a finite number is not served, since a client would draw it.
func TestAStandingValueThatIsNotANumberIsNotServed(t *testing.T) {
	for _, v := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		state := &pb.SearchState{
			Head: measuredHead(&cli.MeasureResult{
				Metric: &cli.Metric{Name: "tput", Direction: cli.Direction_DIRECTION_MAXIMIZE}, Value: v,
			}),
			Series: []*pb.ObjectiveSeries{{Objective: "tput", Points: []*pb.MeasuredPoint{{Value: v, BestSoFar: true}}}},
		}
		got := search.Standings(state)
		if len(got) != 1 || got[0].Name != "tput" || got[0].HeadValue != nil || got[0].BestValue != nil {
			t.Fatalf("a head and a best of %v serve %v", v, got)
		}
	}
}

// Whatever the head, the bounds and the series hold, the standings name each objective once, never serve an
// unnamed one or a value that is not finite, and stay within the contract's cap.
func TestStandingsNameEachObjectiveOnceWithFiniteValues(t *testing.T) {
	prop := func(measured, bounded, serial []uint8, values []float64) bool {
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
		m := &cli.MeasureResult{Metric: &cli.Metric{Name: "a", Direction: cli.Direction_DIRECTION_MAXIMIZE}, Value: value(0)}
		var metrics []*cli.Metric
		declaredOnce := map[string]bool{}
		for i, name := range names(measured) {
			// A task declares each objective once.
			if declaredOnce[name] {
				continue
			}
			declaredOnce[name] = true
			m.Observations = append(m.Observations, &cli.Observation{Name: name, Value: ptr(value(i + 1))})
			metrics = append(metrics, &cli.Metric{Name: name, Direction: cli.Direction_DIRECTION_MAXIMIZE})
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
		inSeries := map[string]bool{}
		for i, name := range names(serial) {
			if !inSeries[name] {
				inSeries[name] = true
				state.Series = append(state.Series, seriesOf(name, value(i), value(i+1)))
			}
		}
		got := search.Standings(state)
		once := map[string]bool{}
		finite := func(v *float64) bool { return v == nil || (!math.IsNaN(*v) && !math.IsInf(*v, 0)) }
		for _, s := range got {
			if s.Name == "" || once[s.Name] || !finite(s.HeadValue) || !finite(s.BestValue) {
				return false
			}
			once[s.Name] = true
		}
		return len(got) <= 64
	}
	if err := quick.Check(prop, propcheck.Config(t, nil)); err != nil {
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
