package app_test

import (
	"math"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A plan keeps the bound it was created with, an edit replaces it, no other change loses it, and
// the plan reads back the same from the store.
func TestAPlansGainBoundIsKeptReplacedAndNeverLostByAnotherChange(t *testing.T) {
	a := newPlanApp(t)
	ctx := ctxFor(t)
	first := &core.GainBound{Share: 0.05, RelativeTo: core.GainHeadline}
	p, err := a.CreatePlan(ctx, call, "title", "body", first, 0)
	if err != nil || p.GainBound == nil || *p.GainBound != *first {
		t.Fatalf("created = %+v, %v", p, err)
	}
	title := "new title"
	if p, err = a.EditPlan(ctx, call, p.Number, app.PlanChange{Title: &title}); err != nil || p.GainBound == nil || *p.GainBound != *first {
		t.Fatalf("after a title edit = %+v, %v", p, err)
	}
	if p, err = a.CommentPlan(ctx, call, p.Number, "c"); err != nil || p.GainBound == nil || *p.GainBound != *first {
		t.Fatalf("after a comment = %+v, %v", p, err)
	}
	if p, err = a.ClosePlan(ctx, call, p.Number, core.PlanParked, ""); err != nil || p.GainBound == nil || *p.GainBound != *first {
		t.Fatalf("after a close = %+v, %v", p, err)
	}
	second := &core.GainBound{Share: 1.5, RelativeTo: core.GainHeadline}
	if p, err = a.EditPlan(ctx, call, p.Number, app.PlanChange{GainBound: second}); err != nil || p.GainBound == nil || *p.GainBound != *second {
		t.Fatalf("after replacing the bound = %+v, %v", p, err)
	}
	got, err := a.ViewPlan(ctx, scope, p.Number)
	if err != nil || got.Plan.GainBound == nil || *got.Plan.GainBound != *second || got.Plan.Title != title {
		t.Fatalf("read back = %+v, %v", got.Plan, err)
	}
}

// A bound that is not a finite share of a reference is refused, creates nothing and changes nothing.
func TestARefusedGainBoundChangesNothing(t *testing.T) {
	a := newPlanApp(t)
	ctx := ctxFor(t)
	good := &core.GainBound{Share: 0.1, RelativeTo: core.GainHeadline}
	p, err := a.CreatePlan(ctx, call, "title", "body", good, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range []core.GainBound{
		{Share: -0.5, RelativeTo: core.GainHeadline},
		{Share: math.NaN(), RelativeTo: core.GainHeadline},
		{Share: math.Inf(1), RelativeTo: core.GainHeadline},
		{Share: 0.1},
		{Share: 0.1, RelativeTo: "base"},
	} {
		_, err := a.CreatePlan(ctx, call, "another", "", &bad, 0)
		wantInvalid(t, "a create with a bad bound", err)
		_, err = a.EditPlan(ctx, call, p.Number, app.PlanChange{GainBound: &bad})
		wantInvalid(t, "an edit to a bad bound", err)
	}
	plans, err := a.Plans(ctx, scope, core.PlanFilter{})
	if err != nil || len(plans) != 1 || plans[0].GainBound == nil || *plans[0].GainBound != *good {
		t.Fatalf("plans = %+v, %v; want the one plan with its first bound", plans, err)
	}
}
