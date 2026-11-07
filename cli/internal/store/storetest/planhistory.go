package storetest

import (
	"math/rand/v2"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// editOrigin is who makes an edit, at a new instant.
func (e *env) editOrigin(run core.RunID, agent string) core.RecordOrigin {
	return core.RecordOrigin{Run: run, Agent: agent, At: e.now()}
}

// createPlanBy makes a plan with the origin.
func (e *env) createPlanBy(title string, origin core.RecordOrigin) core.Plan {
	e.t.Helper()
	plan, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: title, Origin: origin})
	if err != nil {
		e.t.Fatalf("CreatePlan(%q): %v", title, err)
	}
	return plan
}

// testPlanHistory covers what a plan says of who made it and how its status came to be: its origin,
// and the transitions that are the history of its status.
func testPlanHistory(t *testing.T, newStore New) {
	t.Run("a plan keeps its origin and starts open by it", func(t *testing.T) {
		e := newEnv(t, newStore)
		origin := e.editOrigin("run-a", "lead")
		plan := e.createPlanBy("tile the loop", origin)
		if plan.Origin == nil || !plan.Origin.Same(origin) {
			t.Fatalf("origin = %+v, want %+v", plan.Origin, origin)
		}
		if want := []core.PlanTransition{{Status: core.PlanOpen, Origin: origin}}; !reflect.DeepEqual(plan.Transitions, want) {
			t.Fatalf("transitions = %+v, want the creation as open: %+v", plan.Transitions, want)
		}
		if !plan.CreatedAt.Equal(origin.At) || !plan.UpdatedAt.Equal(origin.At) || plan.Status != core.PlanOpen {
			t.Errorf("plan = %+v, want it open and created and updated at the origin's time", plan)
		}
		if err := plan.CheckHistory(); err != nil {
			t.Errorf("CheckHistory: %v", err)
		}
		outside := e.createPlanBy("by a person", core.RecordOrigin{At: e.now()})
		if outside.Origin == nil || outside.Origin.Run != "" || outside.Origin.Agent != "" {
			t.Errorf("origin of a plan made outside a run = %+v, want one with no run and no agent", outside.Origin)
		}
		got, err := e.s.Plan(e.ctx, plan.Number)
		if err != nil || !reflect.DeepEqual(got, plan) {
			t.Errorf("Plan = %+v, %v; want %+v", got, err, plan)
		}
		snap, err := e.s.Snapshot(e.ctx)
		if err != nil || !reflect.DeepEqual(snap.Plans, []core.Plan{plan, outside}) {
			t.Errorf("Snapshot.Plans = %+v, %v; want the plans with their origins and transitions", snap.Plans, err)
		}
	})

	t.Run("an origin with no time or with a name over the limit is refused, and takes no number", func(t *testing.T) {
		e := newEnv(t, newStore)
		long := strings.Repeat("x", core.MaxOriginBytes+1)
		bad := map[string]core.RecordOrigin{
			"no time":         {Run: "r", Agent: "a"},
			"a long run":      {Run: core.RunID(long), At: e.now()},
			"a long agent":    {Agent: long, At: e.now()},
			"the zero origin": {},
		}
		for name, origin := range bad {
			_, err := e.s.CreatePlan(e.ctx, core.NewPlan{Title: "t", Origin: origin})
			wantKind(t, "CreatePlan with "+name, err, core.FailureInvalidInput)
		}
		plan := e.createPlan("first")
		if plan.Number != 1 {
			t.Fatalf("the plan after the refused ones is number %d, want 1", plan.Number)
		}
		before := snapshot(t, e.s)
		status := core.PlanWon
		for name, origin := range bad {
			_, err := e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Status: &status, Origin: origin})
			wantKind(t, "EditPlan with "+name, err, core.FailureInvalidInput)
		}
		unchanged(t, "an edit with a bad origin", e.s, before)
	})

	t.Run("a status that is not one of the five is refused and changes nothing", func(t *testing.T) {
		e := newEnv(t, newStore)
		plan := e.createPlan("first")
		before := snapshot(t, e.s)
		for _, bad := range []core.PlanStatus{"", "OPEN", "done", "chosen "} {
			_, err := e.s.EditPlan(e.ctx, plan.Number, core.PlanEdit{Status: &bad, Origin: e.editOrigin("r", "a")})
			wantKind(t, "EditPlan to "+string(bad), err, core.FailureInvalidInput)
		}
		unchanged(t, "an edit to a status that does not exist", e.s, before)
	})

	t.Run("an edit that sets the status the plan has adds no transition", func(t *testing.T) {
		e := newEnv(t, newStore)
		plan := e.createPlan("first")
		open := core.PlanOpen
		title := "renamed"
		edit := core.PlanEdit{Title: &title, Status: &open, Origin: e.editOrigin("r2", "lead")}
		edited, err := e.s.EditPlan(e.ctx, plan.Number, edit)
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(edited.Transitions, plan.Transitions) || !edited.UpdatedAt.Equal(edit.Origin.At) {
			t.Errorf("plan = %+v, want the transitions as they were and the update time moved", edited)
		}
	})

	// Over random edit sequences on several plans at once, a plan's transitions are exactly the
	// creation as open and then each edit that set a status the plan did not have, in order, set by
	// that edit's origin, and its status is the last of them. The origin of the plan never changes.
	t.Run("transitions change exactly when the status does", func(t *testing.T) {
		statuses := core.PlanStatuses()
		for seed := range uint64(6) {
			e := newEnv(t, newStore)
			rng := rand.New(rand.NewPCG(seed, 99))
			runs := []core.RunID{"", "run-a", "run-b"}
			agents := []string{"", "lead", "planner"}
			var made []core.RecordOrigin
			var want []core.Plan
			for i := range 3 {
				origin := e.editOrigin(runs[i], agents[i])
				plan := e.createPlanBy("plan", origin)
				made, want = append(made, origin), append(want, plan)
			}
			for step := range 40 {
				i := rng.IntN(len(want))
				edit := core.PlanEdit{Origin: e.editOrigin(runs[rng.IntN(3)], agents[rng.IntN(3)])}
				if rng.IntN(5) < 3 {
					edit.Status = &statuses[rng.IntN(len(statuses))]
				}
				if rng.IntN(3) == 0 {
					title := "title"
					edit.Title = &title
				}
				if rng.IntN(3) == 0 {
					edit.Comment = &core.PlanComment{Text: "c", At: e.now(), Agent: "a"}
				}
				if rng.IntN(4) == 0 {
					edit.GainBound = &core.GainBound{Share: 0.1, RelativeTo: core.GainHeadline}
				}
				model := &want[i]
				if edit.Status != nil && *edit.Status != model.Status {
					model.Status = *edit.Status
					model.Transitions = append(slices.Clone(model.Transitions), core.PlanTransition{Status: *edit.Status, Origin: edit.Origin})
				}
				got, err := e.s.EditPlan(e.ctx, core.PlanNumber(i+1), edit)
				if err != nil {
					t.Fatalf("seed %d step %d: EditPlan: %v", seed, step, err)
				}
				if got.Status != model.Status || !reflect.DeepEqual(got.Transitions, model.Transitions) {
					t.Fatalf("seed %d step %d: plan %d is %s with transitions %+v, want %s with %+v", seed, step, i+1, got.Status, got.Transitions, model.Status, model.Transitions)
				}
				if got.Origin == nil || !got.Origin.Same(made[i]) {
					t.Fatalf("seed %d step %d: origin of plan %d = %+v, want %+v: an edit never changes who made it", seed, step, i+1, got.Origin, made[i])
				}
				if err := got.CheckHistory(); err != nil {
					t.Fatalf("seed %d step %d: CheckHistory: %v", seed, step, err)
				}
				if !got.UpdatedAt.Equal(edit.Origin.At) {
					t.Fatalf("seed %d step %d: updated at %v, want the edit's time %v", seed, step, got.UpdatedAt, edit.Origin.At)
				}
			}
			all, err := e.s.Plans(e.ctx, core.PlanFilter{})
			if err != nil || len(all) != len(want) {
				t.Fatalf("seed %d: Plans = %d, %v", seed, len(all), err)
			}
			for i, p := range all {
				if p.Status != want[i].Status || !reflect.DeepEqual(p.Transitions, want[i].Transitions) {
					t.Errorf("seed %d: plan %d read back is %s with %+v, want %s with %+v", seed, i+1, p.Status, p.Transitions, want[i].Status, want[i].Transitions)
				}
			}
		}
	})
}
