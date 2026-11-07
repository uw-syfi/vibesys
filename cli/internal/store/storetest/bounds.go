package storetest

import (
	"math"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// newBound is a well formed bound for the objective, declared now.
func (e *env) newBound(objective string, value float64) core.NewBound {
	return core.NewBound{
		Origin:    core.RecordOrigin{Run: "r1", Agent: "lead", At: e.now()},
		Objective: objective, Direction: core.Maximize, Value: value, Unit: "tok/s", Note: "memory bandwidth over bytes per token",
	}
}

func (e *env) declare(nb core.NewBound) core.Bound {
	e.t.Helper()
	b, err := e.s.DeclareBound(e.ctx, nb)
	if err != nil {
		e.t.Fatalf("DeclareBound(%+v): %v", nb, err)
	}
	return b
}

func (e *env) bounds() []core.Bound {
	e.t.Helper()
	bounds, err := e.s.Bounds(e.ctx)
	if err != nil {
		e.t.Fatalf("Bounds: %v", err)
	}
	return bounds
}

func testBounds(t *testing.T, newStore New) {
	t.Run("a store with no bound declared lists none", func(t *testing.T) {
		e := newEnv(t, newStore)
		if got := e.bounds(); len(got) != 0 {
			t.Fatalf("Bounds = %+v, want none", got)
		}
	})

	t.Run("places bounds from 1 in the order declared and keeps every version", func(t *testing.T) {
		e := newEnv(t, newStore)
		first := e.newBound("throughput", 15000)
		second := e.newBound("throughput", 12000)
		other := e.newBound("latency", 74)
		other.Direction = core.Minimize
		declared := []core.Bound{e.declare(first), e.declare(second), e.declare(other)}
		for i, b := range declared {
			if b.Seq != uint64(i+1) {
				t.Fatalf("bound %d has place %d: places go in order, without gaps", i, b.Seq)
			}
		}
		if want := (core.Bound{Seq: 1, NewBound: first}); !reflect.DeepEqual(declared[0], want) {
			t.Errorf("declared = %+v, want %+v: nothing is changed on the way in", declared[0], want)
		}
		got := e.bounds()
		if !reflect.DeepEqual(got, declared) {
			t.Fatalf("Bounds = %+v, want %+v: a later declaration of an objective supersedes and never replaces", got, declared)
		}
		stands := core.StandingBounds(got)
		if len(stands) != 2 || stands[0].Seq != 2 || stands[1].Seq != 3 {
			t.Errorf("standing = %+v, want the newest for each objective, in the order of the log", stands)
		}
	})

	t.Run("a refused declaration takes no place", func(t *testing.T) {
		e := newEnv(t, newStore)
		bad := map[string]func(*core.NewBound){
			"an empty objective":     func(b *core.NewBound) { b.Objective = "" },
			"no direction":           func(b *core.NewBound) { b.Direction = "" },
			"a value of zero":        func(b *core.NewBound) { b.Value = 0 },
			"a negative value":       func(b *core.NewBound) { b.Value = -1 },
			"a value that is NaN":    func(b *core.NewBound) { b.Value = math.NaN() },
			"an infinite value":      func(b *core.NewBound) { b.Value = math.Inf(1) },
			"an empty unit":          func(b *core.NewBound) { b.Unit = "" },
			"a unit of two lines":    func(b *core.NewBound) { b.Unit = "tok\ns" },
			"an empty note":          func(b *core.NewBound) { b.Note = " " },
			"a note that is no text": func(b *core.NewBound) { b.Note = "bad\xff" },
			"no time":                func(b *core.NewBound) { b.Origin.At = time.Time{} },
		}
		for name, mutate := range bad {
			nb := e.newBound("throughput", 1)
			mutate(&nb)
			_, err := e.s.DeclareBound(e.ctx, nb)
			wantKind(t, name, err, core.FailureInvalidInput)
		}
		if got := e.declare(e.newBound("throughput", 1)); got.Seq != 1 {
			t.Errorf("the declaration after the refused ones has place %d, want 1", got.Seq)
		}
	})

	t.Run("a snapshot holds the bounds", func(t *testing.T) {
		e := newEnv(t, newStore)
		b := e.declare(e.newBound("throughput", 15000))
		snap, err := e.s.Snapshot(e.ctx)
		if err != nil || !reflect.DeepEqual(snap.Bounds, []core.Bound{b}) {
			t.Fatalf("Snapshot.Bounds = %+v, %v; want %+v", snap.Bounds, err, b)
		}
	})

	t.Run("what a read returns is a copy", func(t *testing.T) {
		e := newEnv(t, newStore)
		e.declare(e.newBound("throughput", 15000))
		got := e.bounds()
		got[0].Note = "changed"
		got[0].Value = 1
		if again := e.bounds(); again[0].Note == "changed" || again[0].Value == 1 {
			t.Fatalf("a change to the answer changed the store: %+v", again)
		}
	})
}
