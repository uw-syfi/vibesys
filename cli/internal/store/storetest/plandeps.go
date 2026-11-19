package storetest

import (
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// testPlanDependencies covers a plan's kind and the plans it depends on: what the store keeps, what
// it refuses, and the ready and kind filters.
func testPlanDependencies(t *testing.T, newStore New) {
	t.Run("a plan keeps its kind and its dependencies ascending and each once", func(t *testing.T) {
		e := newEnv(t, newStore)
		e.createPlan("one")
		e.createPlan("two")
		plan, err := e.s.CreatePlan(e.ctx, core.NewPlan{
			Title: "merge", Kind: core.PlanIntegration, DependsOn: []core.PlanNumber{2, 1, 2}, Origin: core.RecordOrigin{At: e.now()},
		})
		if err != nil {
			t.Fatal(err)
		}
		if plan.Kind != core.PlanIntegration || !reflect.DeepEqual(plan.DependsOn, []core.PlanNumber{1, 2}) {
			t.Fatalf("CreatePlan = kind %q, dependencies %v; want integration and [1 2]", plan.Kind, plan.DependsOn)
		}
		got, err := e.s.Plan(e.ctx, plan.Number)
		if err != nil || !reflect.DeepEqual(got, plan) {
			t.Errorf("Plan = %+v, %v; want %+v", got, err, plan)
		}
		kind, none := core.PlanProbe, []core.PlanNumber{}
		cleared, err := e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Kind: &kind, DependsOn: &none, Origin: core.RecordOrigin{At: e.now()}})
		if err != nil {
			t.Fatal(err)
		}
		if cleared.Kind != core.PlanProbe || cleared.DependsOn != nil {
			t.Errorf("EditPlan = kind %q, dependencies %v; want probe and none", cleared.Kind, cleared.DependsOn)
		}
		title := "renamed"
		kept, err := e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Title: &title, Origin: core.RecordOrigin{At: e.now()}})
		if err != nil || kept.Kind != core.PlanProbe {
			t.Errorf("an edit that names no kind left kind %q, %v; want probe", kept.Kind, err)
		}
	})

	t.Run("an unknown plan, the plan itself, a cycle and an unknown kind are refused and change nothing", func(t *testing.T) {
		e := newEnv(t, newStore)
		for _, title := range []string{"one", "two", "three"} {
			e.createPlan(title)
		}
		edit := func(n core.PlanNumber, deps ...core.PlanNumber) error {
			_, err := e.s.EditPlan(e.ctx, n, core.PlanEdit{DependsOn: &deps, Origin: core.RecordOrigin{At: e.now()}})
			return err
		}
		if err := edit(2, 1); err != nil {
			t.Fatal(err)
		}
		if err := edit(3, 2); err != nil {
			t.Fatal(err)
		}
		before := snapshot(t, e.s)
		_, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: "t", DependsOn: []core.PlanNumber{9}, Origin: core.RecordOrigin{At: e.now()}})
		wantKind(t, "CreatePlan that depends on plan 9", err, core.FailureNotFound)
		_, err = e.s.CreatePlan(e.ctx, core.NewPlan{Title: "t", DependsOn: []core.PlanNumber{4}, Origin: core.RecordOrigin{At: e.now()}})
		wantKind(t, "CreatePlan that depends on the number it would take", err, core.FailureNotFound)
		wantKind(t, "EditPlan to depend on plan 9", edit(1, 9), core.FailureNotFound)
		wantKind(t, "EditPlan to depend on itself", edit(1, 1), core.FailureInvalidInput)
		wantKind(t, "EditPlan to close a cycle of three", edit(1, 3), core.FailureInvalidInput)
		wantKind(t, "EditPlan to close a cycle of two", edit(2, 3), core.FailureInvalidInput)
		bad := core.PlanKind("compiler")
		_, err = e.s.CreatePlan(e.ctx, core.NewPlan{Title: "t", Kind: bad, Origin: core.RecordOrigin{At: e.now()}})
		wantKind(t, "CreatePlan of an unknown kind", err, core.FailureInvalidInput)
		_, err = e.s.EditPlan(e.ctx, 1, core.PlanEdit{Kind: &bad, Origin: core.RecordOrigin{At: e.now()}})
		wantKind(t, "EditPlan to an unknown kind", err, core.FailureInvalidInput)
		unchanged(t, "a refused dependency or kind", e.s, before)
		if p := e.createPlan("four"); p.Number != 4 {
			t.Errorf("the plan after the refused ones is number %d, want 4", p.Number)
		}
	})

	// Over random sequences of creates, dependency edits and status changes, the store accepts a set
	// of dependencies exactly when each is a plan there is, none is the plan itself and none reaches
	// back to it, which a model decides on its own; no cycle is ever stored; and the ready and kind
	// filters list what the model says.
	t.Run("no cycle is ever accepted, and ready and kind follow the model", func(t *testing.T) {
		statuses := core.PlanStatuses()
		kinds := append([]core.PlanKind{""}, core.PlanKinds()...)
		for seed := range uint64(8) {
			e := newEnv(t, newStore)
			rng := rand.New(rand.NewPCG(seed, 4325))
			var model []core.Plan // plan n is model[n-1]
			someDeps := func() []core.PlanNumber {
				var deps []core.PlanNumber
				for range rng.IntN(4) {
					deps = append(deps, core.PlanNumber(1+rng.IntN(len(model)+2)))
				}
				return deps
			}
			for step := range 120 {
				switch op := rng.IntN(4); {
				case op == 0 || len(model) == 0:
					np := core.NewPlan{Title: "p", Kind: kinds[rng.IntN(len(kinds))], DependsOn: someDeps(), Origin: core.RecordOrigin{At: e.now()}}
					number := core.PlanNumber(len(model) + 1)
					ok := modelAccepts(model, number, np.DependsOn)
					got, err := e.s.CreatePlan(e.ctx, np)
					if (err == nil) != ok {
						t.Fatalf("seed %d step %d: CreatePlan depending on %v = %v, the model accepts it: %v", seed, step, np.DependsOn, err, ok)
					}
					if ok {
						model = append(model, core.Plan{Number: number, Status: core.PlanOpen, Kind: np.Kind, DependsOn: modelNormal(np.DependsOn)})
						if got.Number != number {
							t.Fatalf("seed %d step %d: number %d, want %d", seed, step, got.Number, number)
						}
					}
				case op == 1:
					number := core.PlanNumber(1 + rng.IntN(len(model)))
					deps := someDeps()
					ok := modelAccepts(model, number, deps)
					_, err := e.s.EditPlan(e.ctx, number, core.PlanEdit{DependsOn: &deps, Origin: core.RecordOrigin{At: e.now()}})
					if (err == nil) != ok {
						t.Fatalf("seed %d step %d: plan %d to depend on %v = %v, the model accepts it: %v (plans %+v)", seed, step, number, deps, err, ok, model)
					}
					if ok {
						model[number-1].DependsOn = modelNormal(deps)
					}
				default:
					number := core.PlanNumber(1 + rng.IntN(len(model)))
					status := statuses[rng.IntN(len(statuses))]
					if _, err := e.s.EditPlan(e.ctx, number, core.PlanEdit{Status: &status, Origin: core.RecordOrigin{At: e.now()}}); err != nil {
						t.Fatalf("seed %d step %d: EditPlan status: %v", seed, step, err)
					}
					model[number-1].Status = status
				}
				checkAgainstModel(t, e, model, kinds[rng.IntN(len(kinds))])
			}
		}
	})
}

// checkAgainstModel reads every plan and the ready and kind filters, and fails unless they are what
// the model holds and no plan reaches itself through its dependencies.
func checkAgainstModel(t *testing.T, e *env, model []core.Plan, kind core.PlanKind) {
	t.Helper()
	all, err := e.s.Plans(e.ctx, core.PlanFilter{})
	if err != nil || len(all) != len(model) {
		t.Fatalf("Plans = %d, %v; want %d", len(all), err, len(model))
	}
	for i, p := range all {
		if p.Status != model[i].Status || p.Kind != model[i].Kind || !reflect.DeepEqual(p.DependsOn, model[i].DependsOn) {
			t.Fatalf("plan %d = %s %q %v, want %s %q %v", p.Number, p.Status, p.Kind, p.DependsOn, model[i].Status, model[i].Kind, model[i].DependsOn)
		}
		if modelReaches(model, p.DependsOn, p.Number) {
			t.Fatalf("plan %d depends on itself through %v: a cycle was stored", p.Number, p.DependsOn)
		}
	}
	ready, err := e.s.Plans(e.ctx, core.PlanFilter{Ready: true})
	if err != nil {
		t.Fatal(err)
	}
	var want []core.PlanNumber
	for _, p := range model {
		waiting := slices.ContainsFunc(p.DependsOn, func(d core.PlanNumber) bool { return model[d-1].Status != core.PlanWon })
		if p.Status == core.PlanOpen && !waiting {
			want = append(want, p.Number)
		}
	}
	if got := numbersOf(ready); !slices.Equal(got, want) {
		t.Fatalf("ready plans = %v, want %v (plans %+v)", got, want, model)
	}
	if kind == "" {
		return
	}
	ofKind, err := e.s.Plans(e.ctx, core.PlanFilter{Kinds: []core.PlanKind{kind}})
	if err != nil {
		t.Fatal(err)
	}
	want = nil
	for _, p := range model {
		if p.Kind == kind {
			want = append(want, p.Number)
		}
	}
	if got := numbersOf(ofKind); !slices.Equal(got, want) {
		t.Fatalf("plans of kind %s = %v, want %v", kind, got, want)
	}
}

func numbersOf(plans []core.Plan) []core.PlanNumber {
	var out []core.PlanNumber
	for _, p := range plans {
		out = append(out, p.Number)
	}
	return out
}

// modelAccepts is the model's own answer to whether plan number may depend on deps.
func modelAccepts(model []core.Plan, number core.PlanNumber, deps []core.PlanNumber) bool {
	for _, d := range deps {
		if d == number || int(d) > len(model) {
			return false
		}
	}
	return !modelReaches(model, deps, number)
}

// modelReaches says whether the plans in from, or any plan they depend on, is target, following
// the model's dependencies breadth first.
func modelReaches(model []core.Plan, from []core.PlanNumber, target core.PlanNumber) bool {
	seen := map[core.PlanNumber]bool{}
	queue := slices.Clone(from)
	for len(queue) > 0 {
		n := queue[0]
		queue = queue[1:]
		if n == target {
			return true
		}
		if seen[n] || int(n) > len(model) {
			continue
		}
		seen[n] = true
		queue = append(queue, model[n-1].DependsOn...)
	}
	return false
}

// modelNormal is the form the store keeps dependencies in: ascending, each once, nil for none.
func modelNormal(deps []core.PlanNumber) []core.PlanNumber {
	if len(deps) == 0 {
		return nil
	}
	set := map[core.PlanNumber]bool{}
	var out []core.PlanNumber
	for _, d := range deps {
		if !set[d] {
			set[d] = true
			out = append(out, d)
		}
	}
	slices.Sort(out)
	return out
}
