package core_test

import (
	"errors"
	"math"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var servingObjectives = []core.Metric{
	{Name: "tok_s", Direction: core.Maximize},
	{Name: "ttft_ms", Direction: core.Minimize},
}

var declaredAt = core.RecordOrigin{Run: "run-1", Agent: "lead", At: time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC)}

func statement(objective string, value float64) core.BoundStatement {
	return core.BoundStatement{Objective: objective, Value: value, Unit: "tok/s", Note: "memory bandwidth over bytes per token"}
}

func invalidInput(t *testing.T, what string, err error) *core.Failure {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
		t.Fatalf("%s: err = %v, want invalid input", what, err)
	}
	return f
}

// The direction of a bound is the task's, whatever the statement: it takes the direction of the
// objective it names, and the origin it is given.
func TestABoundTakesTheDirectionOfItsObjectiveFromTheTask(t *testing.T) {
	r := newRand(21)
	for range properties {
		objective := servingObjectives[r.IntN(len(servingObjectives))]
		value := 1 + r.Float64()*1e6
		got, err := core.DecideBound(servingObjectives, statement(objective.Name, value), declaredAt)
		if err != nil {
			t.Fatalf("%s %v: %v", objective.Name, value, err)
		}
		if got.Direction != objective.Direction || got.Objective != objective.Name || got.Value != value || got.Origin != declaredAt {
			t.Fatalf("bound = %+v for %+v", got, objective)
		}
		if err := got.Check(); err != nil {
			t.Fatalf("a decided bound fails its own check: %v", err)
		}
	}
}

// An objective the task does not declare is refused with an error that names it, and lists the ones
// the task does declare.
func TestAnUnknownObjectiveIsRefusedNamingItAndTheKnownOnes(t *testing.T) {
	_, err := core.DecideBound(servingObjectives, statement("throughput", 10), declaredAt)
	f := invalidInput(t, "an unknown objective", err)
	for _, want := range []string{`"throughput"`, `"tok_s"`, `"ttft_ms"`, "objective"} {
		if !strings.Contains(f.Message, want) {
			t.Errorf("message %q does not mention %s", f.Message, want)
		}
	}
	if _, err := core.DecideBound(nil, statement("tok_s", 10), declaredAt); err == nil {
		t.Error("a task with no objectives accepted a bound")
	}
	// Names are exact: a case or a space makes another name.
	for _, name := range []string{"TOK_S", " tok_s", "tok_s ", "tok"} {
		if _, err := core.DecideBound(servingObjectives, statement(name, 10), declaredAt); err == nil {
			t.Errorf("objective %q was accepted", name)
		}
	}
}

// Whatever the value, a statement is accepted exactly when the value is finite and above zero, and a
// refusal names the field.
func TestOnlyAFiniteValueAboveZeroIsABound(t *testing.T) {
	values := []float64{0, -0.0, -1, math.NaN(), math.Inf(1), math.Inf(-1), -math.MaxFloat64}
	r := newRand(5)
	for range properties {
		values = append(values, r.NormFloat64()*math.Pow(10, float64(r.IntN(12)-6)))
	}
	for _, v := range values {
		_, err := core.DecideBound(servingObjectives, statement("tok_s", v), declaredAt)
		finiteAboveZero := !math.IsNaN(v) && !math.IsInf(v, 0) && v > 0
		if finiteAboveZero != (err == nil) {
			t.Fatalf("value %v: accepted %v, want %v (%v)", v, err == nil, finiteAboveZero, err)
		}
		if err != nil && !strings.Contains(invalidInput(t, "value", err).Message, "value") {
			t.Fatalf("the refusal of %v does not name the value: %v", v, err)
		}
	}
}

func TestTheTextOfABoundIsCheckedAndNamesTheField(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(*core.BoundStatement)
		field  string
	}{
		{"an empty unit", func(s *core.BoundStatement) { s.Unit = "" }, "unit"},
		{"a blank unit", func(s *core.BoundStatement) { s.Unit = "  " }, "unit"},
		{"a unit of two lines", func(s *core.BoundStatement) { s.Unit = "tok\ns" }, "unit"},
		{"a unit past its limit", func(s *core.BoundStatement) { s.Unit = strings.Repeat("u", core.MaxBoundUnitBytes+1) }, "unit"},
		{"an empty note", func(s *core.BoundStatement) { s.Note = "" }, "note"},
		{"a note that is not UTF-8", func(s *core.BoundStatement) { s.Note = "bad\xff" }, "note"},
		{"a note past its limit", func(s *core.BoundStatement) { s.Note = strings.Repeat("n", core.MaxBoundNoteBytes+1) }, "note"},
		{"an empty objective", func(s *core.BoundStatement) { s.Objective = "" }, "objective"},
	} {
		s := statement("tok_s", 10)
		tc.mutate(&s)
		_, err := core.DecideBound(servingObjectives, s, declaredAt)
		if f := invalidInput(t, tc.name, err); !strings.Contains(f.Message, tc.field) {
			t.Errorf("%s: message %q does not name %s", tc.name, f.Message, tc.field)
		}
	}
	// The limits themselves are accepted, and a unit loses the space around it.
	s := statement("tok_s", 10)
	s.Unit, s.Note = " "+strings.Repeat("u", core.MaxBoundUnitBytes-2)+" ", strings.Repeat("n", core.MaxBoundNoteBytes)
	got, err := core.DecideBound(servingObjectives, s, declaredAt)
	if err != nil || got.Unit != strings.Repeat("u", core.MaxBoundUnitBytes-2) {
		t.Fatalf("a note at its limit: %+v, %v", got, err)
	}
}

func TestAnOriginPastItsLimitOrWithNoTimeIsRefused(t *testing.T) {
	long := strings.Repeat("a", core.MaxOriginBytes+1)
	for name, origin := range map[string]core.RecordOrigin{
		"a long run":   {Run: core.RunID(long), At: declaredAt.At},
		"a long agent": {Agent: long, At: declaredAt.At},
		"no time":      {Run: "r"},
	} {
		if _, err := core.DecideBound(servingObjectives, statement("tok_s", 10), origin); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
	if _, err := core.DecideBound(servingObjectives, statement("tok_s", 10), core.RecordOrigin{At: declaredAt.At}); err != nil {
		t.Errorf("a call from outside a run was refused: %v", err)
	}
}

// StandingBounds picks, for each objective, the newest declaration, in the order of the log, and
// is what a reader sees whatever the order the objectives were declared in.
func TestTheStandingBoundOfAnObjectiveIsTheNewest(t *testing.T) {
	r := newRand(33)
	for range properties {
		var log []core.Bound
		newest := map[string]uint64{}
		for seq := uint64(1); seq <= uint64(r.IntN(12)); seq++ {
			name := servingObjectives[r.IntN(len(servingObjectives))].Name
			log = append(log, core.Bound{Seq: seq, NewBound: core.NewBound{Objective: name}})
			newest[name] = seq
		}
		got := core.StandingBounds(log)
		if len(got) != len(newest) {
			t.Fatalf("%d standing bounds for %d objectives: %+v", len(got), len(newest), got)
		}
		for _, b := range got {
			if b.Seq != newest[b.Objective] {
				t.Fatalf("the standing bound of %s is %d, want %d", b.Objective, b.Seq, newest[b.Objective])
			}
		}
		if !slices.IsSortedFunc(got, func(a, b core.Bound) int { return int(a.Seq) - int(b.Seq) }) {
			t.Fatalf("standing bounds are not in the order of the log: %+v", got)
		}
	}
}
