package search_test

import (
	"fmt"
	"math"
	"math/rand"
	"strings"
	"testing"
	"testing/quick"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/propcheck"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

const (
	maximize = cli.Direction_DIRECTION_MAXIMIZE
	minimize = cli.Direction_DIRECTION_MINIMIZE
)

func boundFact(seq uint64, run, objective string, direction cli.Direction, value float64) *cli.TaskFact {
	return &cli.TaskFact{Fact: &cli.TaskFact_Bound{Bound: &cli.ObjectiveBound{
		Seq: seq, Origin: &cli.RecordOrigin{RunId: run, Agent: "lead", At: &timestamppb.Timestamp{Seconds: int64(seq)}},
		Objective: objective, Direction: direction, Value: value, Unit: "u", Note: "derived",
	}}}
}

// headOf is a head whose measurement scores a metric and reports others beside it.
func headOf(metric string, direction cli.Direction, value float64, others map[string]float64, directions map[string]cli.Direction) *cli.TaskFact {
	m := &cli.MeasureResult{Metric: &cli.Metric{Name: metric, Direction: direction}, Value: value}
	if len(directions) > 0 {
		m.Objectives = &cli.Objectives{Metrics: []*cli.Metric{{Name: metric, Direction: direction}}}
	}
	for name, v := range others {
		m.Observations = append(m.Observations, &cli.Observation{Name: name, Value: proto.Float64(v)})
		if d, ok := directions[name]; ok {
			m.Objectives.Metrics = append(m.Objectives.Metrics, &cli.Metric{Name: name, Direction: d})
		}
	}
	return &cli.TaskFact{Fact: &cli.TaskFact_Head{Head: &cli.Head{State: &cli.Head_Current{Current: &cli.CurrentResult{
		Head: strings.Repeat("a", 40), Result: &cli.Result{JobId: "j1", Outcome: &cli.Result_Measure{Measure: m}},
	}}}}}
}

func project(t *testing.T, facts ...*cli.TaskFact) *pb.SearchState {
	t.Helper()
	// Identity and health are set before the cap, as the daemon sets them: the byte budget counts them.
	s := search.Cap(search.Fold(facts), &pb.SearchState{RunId: "r", Revision: 1, Health: &pb.SearchHealth{State: &pb.SearchHealth_LiveSince{LiveSince: &timestamppb.Timestamp{Seconds: 1}}}})
	if err := protovalidate.Validate(s); err != nil {
		t.Fatalf("the projected state breaks its own rules: %v", err)
	}
	return s
}

func byObjective(s *pb.SearchState, objective string) *pb.ObjectiveBounds {
	for _, b := range s.Bounds {
		if b.Objective == objective {
			return b
		}
	}
	return nil
}

// A value past the bound has gone past it in the objective's own direction: more is better for both
// directions, 100 is at the bound, and the share does not depend on the unit.
func TestPercentIsMoreForABetterValueInEitherDirection(t *testing.T) {
	positive := func(x float64) float64 { return math.Exp(math.Mod(x, 20)) } // from about 2e-9 to 5e8
	property := func(a, b, c, k float64) bool {
		bound, v1, v2, scale := positive(a), positive(b), positive(c), positive(k)
		for _, d := range []cli.Direction{maximize, minimize} {
			p1, ok1 := search.PercentOfBound(d, bound, v1)
			p2, ok2 := search.PercentOfBound(d, bound, v2)
			scaled, ok3 := search.PercentOfBound(d, bound*scale, v1*scale)
			at, ok4 := search.PercentOfBound(d, bound, bound)
			if !ok1 || !ok2 || !ok3 || !ok4 || p1 <= 0 || math.IsInf(p1, 0) || math.Abs(scaled-p1) > 1e-9*p1 || math.Abs(at-100) > 1e-9 {
				return false
			}
			better := v1 > v2
			if d == minimize {
				better = v1 < v2
			}
			if v1 != v2 && better != (p1 > p2) {
				return false
			}
			past := v1 > bound
			if d == minimize {
				past = v1 < bound
			}
			if v1 != bound && past != (p1 > 100) {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, propcheck.Config(t, &quick.Config{MaxCount: 2000})); err != nil {
		t.Fatal(err)
	}
	if p, _ := search.PercentOfBound(maximize, 15600, 11076); math.Abs(p-71) > 0.01 {
		t.Errorf("11076 of 15600 tok/s = %v%%, want 71%%", p)
	}
	if p, _ := search.PercentOfBound(minimize, 74, 118); math.Abs(p-62.71) > 0.01 {
		t.Errorf("118 ms against 74 ms = %v%%, want 62.7%%", p)
	}
}

func TestPercentIsUndefinedWhereNothingMeaningfulIsGiven(t *testing.T) {
	nan, inf := math.NaN(), math.Inf(1)
	for _, tc := range []struct {
		name        string
		d           cli.Direction
		bound, have float64
	}{
		{"no direction", cli.Direction_DIRECTION_UNSPECIFIED, 1, 1},
		{"an unknown direction", 9, 1, 1},
		{"a bound of zero", maximize, 0, 1},
		{"a negative bound", minimize, -1, 1},
		{"a value of zero", minimize, 1, 0},
		{"a negative value", maximize, 1, -2},
		{"a NaN bound", maximize, nan, 1},
		{"a NaN value", minimize, 1, nan},
		{"an infinite bound", maximize, inf, 1},
		{"an infinite value", minimize, 1, inf},
		{"a share that overflows", maximize, math.SmallestNonzeroFloat64, math.MaxFloat64},
		{"a share that underflows", maximize, math.MaxFloat64, math.SmallestNonzeroFloat64},
	} {
		if p, ok := search.PercentOfBound(tc.d, tc.bound, tc.have); ok {
			t.Errorf("%s: %v%%, want none", tc.name, p)
		}
	}
}

// The view of a run holds the bounds that run declared and those a person declared outside any run, one
// entry per objective by name, each with every version oldest first and the standing bound last. The
// store holds the bounds of every run of the task, so another run's are not this run's.
func TestTheViewHoldsThisRunsBoundsPerObjective(t *testing.T) {
	s := project(t,
		boundFact(3, "r", "tok_s", maximize, 12000),
		boundFact(1, "r", "tok_s", maximize, 15600),
		boundFact(2, "other", "tok_s", maximize, 99999),
		boundFact(4, "", "ttft_ms", minimize, 1),
		boundFact(5, "r", "ttft_ms", minimize, 74),
		boundFact(6, "r", "tok_s", maximize, 13000),
	)
	if len(s.Bounds) != 2 || s.Bounds[0].Objective != "tok_s" || s.Bounds[1].Objective != "ttft_ms" {
		t.Fatalf("bounds = %v, want tok_s and ttft_ms, by name", s.Bounds)
	}
	tok := byObjective(s, "tok_s")
	var seqs []uint64
	for _, v := range tok.Versions {
		seqs = append(seqs, v.Seq)
	}
	if fmt.Sprint(seqs) != "[1 3 6]" || tok.Direction != maximize || tok.Versions[2].Value != 13000 {
		t.Fatalf("tok_s versions %v, direction %v, standing %v; want [1 3 6], maximize and 13000 standing", seqs, tok.Direction, tok.Versions[2].GetValue())
	}
	lat := byObjective(s, "ttft_ms")
	if len(lat.Versions) != 2 || lat.Versions[0].Origin.RunId != "" || lat.Versions[1].Value != 74 || lat.Direction != minimize {
		t.Fatalf("ttft_ms = %v: want the person's bound (no run) and then the run's standing 74", lat)
	}
	if s.Truncated {
		t.Error("a view that left nothing of this run out is marked truncated")
	}
	if none := project(t, boundFact(1, "other", "tok_s", maximize, 1)); len(none.Bounds) != 0 {
		t.Errorf("a run with no bounds of its own shows %v", none.Bounds)
	}
}

// A bound a person declared outside any run stands for every run of the task, and stands as the
// standing bound when it is the newest declared for its objective: a person's correction of what the
// agents derived is the bound the head is measured against.
func TestAPersonsBoundOutsideAnyRunStandsForTheRun(t *testing.T) {
	s := project(t,
		boundFact(1, "r", "tok_s", maximize, 20000),
		boundFact(2, "", "tok_s", maximize, 15000),
		headOf("tok_s", maximize, 7500, nil, nil),
	)
	tok := byObjective(s, "tok_s")
	if tok == nil || len(tok.Versions) != 2 || tok.Versions[1].Value != 15000 || math.Abs(tok.GetPercentOfBound()-50) > 1e-9 {
		t.Fatalf("tok_s = %v, want the person's 15000 standing and the head at 50 percent of it", tok)
	}
	if only := byObjective(project(t, boundFact(1, "", "ttft_ms", minimize, 74)), "ttft_ms"); only == nil {
		t.Error("a bound declared outside any run, with no agent's, is not shown")
	}
}

// The percent is of the head's value for the objective, and only when there is one to give.
func TestTheHeadIsComparedWithTheStandingBound(t *testing.T) {
	bounds := []*cli.TaskFact{
		boundFact(1, "r", "tok_s", maximize, 20000),
		boundFact(2, "r", "tok_s", maximize, 15600),
		boundFact(3, "r", "ttft_ms", minimize, 74),
		boundFact(4, "r", "mem_gb", minimize, 40),
	}
	t.Run("the scored metric and an observation, each against its standing bound", func(t *testing.T) {
		s := project(t, append(bounds, headOf("tok_s", maximize, 11076, map[string]float64{"ttft_ms": 118, "mem_gb": 61},
			map[string]cli.Direction{"ttft_ms": minimize, "mem_gb": minimize}))...)
		tok, lat, mem := byObjective(s, "tok_s"), byObjective(s, "ttft_ms"), byObjective(s, "mem_gb")
		if tok.GetHeadValue() != 11076 || math.Abs(tok.GetPercentOfBound()-71) > 0.01 {
			t.Errorf("tok_s head %v, percent %v; want 11076 and 71 of the standing 15600, not the earlier 20000", tok.GetHeadValue(), tok.GetPercentOfBound())
		}
		if lat.GetHeadValue() != 118 || math.Abs(lat.GetPercentOfBound()-62.71) > 0.01 || math.Abs(mem.GetPercentOfBound()-65.57) > 0.01 {
			t.Errorf("ttft_ms head %v percent %v, mem_gb percent %v", lat.GetHeadValue(), lat.GetPercentOfBound(), mem.GetPercentOfBound())
		}
	})
	t.Run("an objective the measurement reports without a direction takes the bound's", func(t *testing.T) {
		s := project(t, append(bounds, headOf("tok_s", maximize, 8000, map[string]float64{"ttft_ms": 148}, nil))...)
		if got := byObjective(s, "ttft_ms").GetPercentOfBound(); math.Abs(got-50) > 1e-9 {
			t.Errorf("ttft_ms percent = %v, want 50", got)
		}
	})
	t.Run("a head that measured the objective the other way is not compared", func(t *testing.T) {
		s := project(t, append(bounds, headOf("tok_s", minimize, 11076, map[string]float64{"ttft_ms": 118}, map[string]cli.Direction{"ttft_ms": maximize}))...)
		if tok, lat := byObjective(s, "tok_s"), byObjective(s, "ttft_ms"); tok.HeadValue != nil || tok.PercentOfBound != nil || lat.HeadValue != nil || lat.PercentOfBound != nil {
			t.Errorf("tok_s %v, ttft_ms %v: a value measured in the other direction has no share of the bound", tok, lat)
		}
	})
	t.Run("no head, or none that reports the objective, gives none", func(t *testing.T) {
		if s := project(t, bounds...); byObjective(s, "tok_s").PercentOfBound != nil || byObjective(s, "tok_s").HeadValue != nil {
			t.Errorf("a run with no head shows %v", byObjective(s, "tok_s"))
		}
		s := project(t, append(bounds, headOf("tok_s", maximize, 11076, nil, nil))...)
		if lat := byObjective(s, "ttft_ms"); lat.HeadValue != nil || lat.PercentOfBound != nil {
			t.Errorf("a head that reports no ttft_ms gives ttft_ms %v", lat)
		}
	})
	t.Run("a head past the bound is over 100", func(t *testing.T) {
		s := project(t, append(bounds, headOf("tok_s", maximize, 17000, nil, nil))...)
		if got := byObjective(s, "tok_s").GetPercentOfBound(); got <= 100 {
			t.Errorf("percent = %v for a head past the bound, want over 100", got)
		}
	})
}

// What the view cannot carry is left out oldest first and says so: versions past the limit, objectives
// past the limit, and versions in another direction than the standing one.
func TestWhatTheViewLeavesOutOfTheBoundsIsMarkedTruncated(t *testing.T) {
	var facts []*cli.TaskFact
	for seq := uint64(1); seq <= 64; seq++ {
		facts = append(facts, boundFact(seq, "r", "tok_s", maximize, float64(seq)))
	}
	if s := project(t, facts...); len(byObjective(s, "tok_s").Versions) != 64 || s.Truncated {
		t.Fatalf("exactly 64 versions: %d kept, truncated %v", len(byObjective(s, "tok_s").Versions), s.Truncated)
	}
	facts = append(facts, boundFact(65, "r", "tok_s", maximize, 65))
	s := project(t, facts...)
	if v := byObjective(s, "tok_s").Versions; len(v) != 64 || v[0].Seq != 2 || v[63].Seq != 65 || !s.Truncated {
		t.Fatalf("65 versions: kept %d from %d to %d, truncated %v; want the newest 64", len(v), v[0].Seq, v[len(v)-1].Seq, s.Truncated)
	}

	var many []*cli.TaskFact
	for i := 0; i <= 64; i++ {
		many = append(many, boundFact(uint64(i+1), "r", fmt.Sprintf("objective-%02d", i), maximize, 1))
	}
	s = project(t, many...)
	if len(s.Bounds) != 64 || byObjective(s, "objective-00") != nil || byObjective(s, "objective-64") == nil || !s.Truncated {
		t.Fatalf("65 objectives: %d kept, truncated %v; want the 64 with the newest bounds", len(s.Bounds), s.Truncated)
	}

	s = project(t, boundFact(1, "r", "tok_s", minimize, 5), boundFact(2, "r", "tok_s", maximize, 6), boundFact(3, "r", "tok_s", maximize, 7))
	if v := byObjective(s, "tok_s").Versions; len(v) != 2 || v[0].Seq != 2 || byObjective(s, "tok_s").Direction != maximize || !s.Truncated {
		t.Fatalf("a direction that changed: versions %v, truncated %v; want only those in the standing direction", v, s.Truncated)
	}
}

// Whatever the bounds, the runs and the head, the projection satisfies the contract's rules and shows
// for each objective exactly what the run declared: its versions in order of declaration and the newest
// standing.
func TestEveryProjectionOfBoundsHoldsTheContractsRules(t *testing.T) {
	r := rand.New(rand.NewSource(77))
	objectives := []string{"tok_s", "ttft_ms", "mem_gb"}
	directions := map[string]cli.Direction{"tok_s": maximize, "ttft_ms": minimize, "mem_gb": minimize}
	for range 300 {
		var facts []*cli.TaskFact
		declared := map[string][]uint64{}
		for seq := uint64(1); seq <= uint64(r.Intn(30)); seq++ {
			run := []string{"r", "other", ""}[r.Intn(3)]
			o := objectives[r.Intn(len(objectives))]
			facts = append(facts, boundFact(seq, run, o, directions[o], float64(1+r.Intn(1000))))
			if run == "r" || run == "" {
				declared[o] = append(declared[o], seq)
			}
		}
		if r.Intn(2) == 0 {
			facts = append(facts, headOf("tok_s", maximize, float64(r.Intn(20000)), map[string]float64{"ttft_ms": float64(r.Intn(300)), "mem_gb": float64(r.Intn(90))}, nil))
		}
		r.Shuffle(len(facts), func(i, j int) { facts[i], facts[j] = facts[j], facts[i] })
		s := project(t, facts...)
		if len(s.Bounds) != len(declared) {
			t.Fatalf("%d entries for %d objectives the run declared", len(s.Bounds), len(declared))
		}
		for i, b := range s.Bounds {
			if i > 0 && s.Bounds[i-1].Objective >= b.Objective {
				t.Fatalf("entries are not by objective name: %v", s.Bounds)
			}
			var got []uint64
			for _, v := range b.Versions {
				got = append(got, v.Seq)
			}
			if fmt.Sprint(got) != fmt.Sprint(declared[b.Objective]) {
				t.Fatalf("%s: versions %v, declared %v", b.Objective, got, declared[b.Objective])
			}
		}
	}
}

// A run's declared bounds are the last thing the byte budget gives up: a view that is over budget with
// jobs and plans loses those first, and keeps the bounds.
func TestBoundsAreTheLastThingTheByteBudgetDrops(t *testing.T) {
	facts := []*cli.TaskFact{boundFact(1, "r", "tok_s", maximize, 15600), boundFact(2, "r", "ttft_ms", minimize, 74)}
	for i := range 100 {
		facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{
			Number: uint32(i + 1), Body: strings.Repeat("x", 8<<10), CreatedAt: &timestamppb.Timestamp{Seconds: int64(1000 + i)},
		}}})
	}
	s := search.Cap(search.Fold(facts), &pb.SearchState{RunId: "r"})
	if proto.Size(s) > search.MaxStateBytes || !s.Truncated || len(s.Plans) == 100 {
		t.Fatalf("size %d, truncated %v, %d plans: the budget did not drop plans", proto.Size(s), s.Truncated, len(s.Plans))
	}
	if len(s.Bounds) != 2 {
		t.Fatalf("bounds = %v, want both kept while plans were dropped, though the bounds are older", s.Bounds)
	}
}
