package search_test

import (
	"fmt"
	"math/rand"
	"slices"
	"strings"
	"testing"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// These tests state what a unit does to the bounds and the series, from the contract: a unit is a
// label that nothing converts, so a bound is checked against the unit of the numbers it bounds by its
// text, and a number whose unit is not known is neither a match nor a mismatch, and is never served as
// a verified share.

var someUnits = []string{"tok/s", "ms", "µs", "GB/s", "requests per second"}

// unitJob is one measure job of the run, of the objective "rate" counted in unit, with the value.
func unitJob(id string, at int64, value float64, unit string) []*cli.TaskFact {
	m := &cli.MeasureResult{Metric: &cli.Metric{Name: "rate", Direction: maximize, Unit: unit}, Value: value}
	return []*cli.TaskFact{
		{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, Submitters: mine}}},
		{Fact: &cli.TaskFact_Result{Result: &cli.Result{JobId: id, Commit: strings.Repeat("c", 40), RecordedAt: &timestamppb.Timestamp{Seconds: at}, Outcome: &cli.Result_Measure{Measure: m}}}},
	}
}

func unitBound(seq uint64, value float64, unit string) *cli.TaskFact {
	f := boundFact(seq, "r", "rate", maximize, value)
	f.GetBound().Unit = unit
	return f
}

func headOfJob(job []*cli.TaskFact) *cli.Head {
	return &cli.Head{State: &cli.Head_Current{Current: &cli.CurrentResult{Head: strings.Repeat("a", 40), Result: &cli.Result{JobId: job[1].GetResult().GetJobId(), Outcome: job[1].GetResult().GetOutcome()}}}}
}

// Whatever units a bound and the measurements are counted in, the entry says which: the same unit is a
// match and serves its shares, two known units that differ are a mismatch that serves no share and names
// both, and a measurement of no known unit is unknown, never a match, with its shares served as such.
func TestABoundIsCheckedAgainstTheUnitOfItsMeasurements(t *testing.T) {
	r := rand.New(rand.NewSource(11))
	for range 500 {
		boundUnit := someUnits[r.Intn(len(someUnits))]
		measured := append([]string{""}, someUnits...)[r.Intn(len(someUnits)+1)]
		var facts []*cli.TaskFact
		for i := range 1 + r.Intn(5) {
			facts = append(facts, unitJob(fmt.Sprintf("j%d", i), int64(i+1), float64(1+r.Intn(50)), measured)...)
		}
		facts = append(facts, unitBound(1, 100, boundUnit))
		var head *cli.Head
		if r.Intn(2) == 0 {
			head = headOfJob(unitJob("jh", 99, float64(1+r.Intn(50)), measured))
		}
		got, _ := search.Bounds(facts, "r", head)
		if len(got) != 1 {
			t.Fatalf("bounds %v", got)
		}
		b := got[0]
		want := pb.BoundUnitCheck_BOUND_UNIT_CHECK_MATCH
		switch {
		case measured == "":
			want = pb.BoundUnitCheck_BOUND_UNIT_CHECK_UNKNOWN
		case measured != boundUnit:
			want = pb.BoundUnitCheck_BOUND_UNIT_CHECK_MISMATCH
		}
		if b.UnitCheck != want {
			t.Fatalf("bound in %q, measured in %q: check %v, want %v", boundUnit, measured, b.UnitCheck, want)
		}
		if b.Versions[len(b.Versions)-1].Unit != boundUnit || b.MeasuredUnit != measured {
			t.Fatalf("units named %q and %q, want %q and %q", b.Versions[len(b.Versions)-1].Unit, b.MeasuredUnit, boundUnit, measured)
		}
		shared := want != pb.BoundUnitCheck_BOUND_UNIT_CHECK_MISMATCH
		if (b.BestPercentOfBound != nil) != shared || (head != nil && (b.PercentOfBound != nil) != shared) || (head == nil && b.PercentOfBound != nil) {
			t.Fatalf("check %v: head share %v, best share %v", want, b.PercentOfBound, b.BestPercentOfBound)
		}
		if b.BestValue == nil {
			t.Fatal("the best value is served whatever the check says: it is a measurement, and its unit is named")
		}
	}
}

// A bound with nothing measured against it has nothing to check, which is unknown and never a match.
func TestABoundWithNoMeasurementIsUnknown(t *testing.T) {
	got, _ := search.Bounds([]*cli.TaskFact{unitBound(1, 5, "ms")}, "r", nil)
	if len(got) != 1 || got[0].UnitCheck != pb.BoundUnitCheck_BOUND_UNIT_CHECK_UNKNOWN || got[0].MeasuredUnit != "" {
		t.Fatalf("%v", got)
	}
}

// A bound that has no unit (declared before units existed) is as unknown as a measurement without one.
func TestABoundWithoutAUnitIsUnknownAgainstAKnownMeasurement(t *testing.T) {
	facts := append(unitJob("j1", 1, 5, "ms"), unitBound(1, 10, ""))
	got, _ := search.Bounds(facts, "r", nil)
	if got[0].UnitCheck != pb.BoundUnitCheck_BOUND_UNIT_CHECK_UNKNOWN || got[0].MeasuredUnit != "ms" {
		t.Fatalf("%v", got[0])
	}
}

// The head and the best are each checked by their own unit, and the entry is the worst of them: a head in
// another unit than the bound's is a mismatch with no share for the head, while the best, in the bound's
// unit, keeps its share.
func TestTheHeadAndTheBestAreCheckedEachByItsOwnUnit(t *testing.T) {
	facts := append(unitJob("j1", 1, 50, "tok/s"), unitBound(1, 100, "tok/s"))
	head := headOfJob(unitJob("jh", 2, 3, "ms"))
	got, _ := search.Bounds(facts, "r", head)
	b := got[0]
	if b.UnitCheck != pb.BoundUnitCheck_BOUND_UNIT_CHECK_MISMATCH || b.PercentOfBound != nil || b.BestPercentOfBound == nil || b.MeasuredUnit != "tok/s" {
		t.Fatalf("%v", b)
	}
}

// Values counted in different known units never share a series: the series is of the newest known unit
// and holds no point of another, and one with an unknown unit stays and makes the series' unit unknown.
func TestASeriesHoldsOneUnit(t *testing.T) {
	var facts []*cli.TaskFact
	facts = append(facts, unitJob("old", 1, 9, "s")...)
	facts = append(facts, unitJob("mid", 2, 4, "ms")...)
	facts = append(facts, unitJob("new", 3, 6, "ms")...)
	s := project(t, facts...)
	series := seriesNamed(s, "rate")
	if series.Unit != "ms" || !slices.Equal(jobsOf(series), []string{"mid", "new"}) {
		t.Fatalf("unit %q, points %v", series.Unit, jobsOf(series))
	}
	facts = append(facts, unitJob("unknown", 4, 7, "")...)
	series = seriesNamed(project(t, facts...), "rate")
	if series.Unit != "" || !slices.Equal(jobsOf(series), []string{"mid", "new", "unknown"}) {
		t.Fatalf("with an unknown point: unit %q, points %v", series.Unit, jobsOf(series))
	}
}

// A series says its unit exactly when every point shares one.
func TestACommonUnitIsTheOneEveryValueSharesAndNoneOtherwise(t *testing.T) {
	r := rand.New(rand.NewSource(3))
	pool := append([]string{""}, someUnits[:3]...)
	for range 1000 {
		units := make([]string, r.Intn(6))
		for i := range units {
			units[i] = pool[r.Intn(len(pool))]
		}
		want := ""
		if len(units) > 0 && !slices.ContainsFunc(units, func(u string) bool { return u != units[0] }) {
			want = units[0]
		}
		if got := search.CommonUnit(units); got != want {
			t.Fatalf("%q: %q, want %q", units, got, want)
		}
	}
}
