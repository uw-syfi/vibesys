package wire_test

import (
	"math"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

func anyGainPlans(n int, bound func(i int) *core.GainBound) []core.Plan {
	var plans []core.Plan
	for i := range n {
		plans = append(plans, core.Plan{
			Number: core.PlanNumber(i + 1), Title: "t", Status: core.PlanStatuses()[i%5], GainBound: bound(i),
		})
	}
	return plans
}

// The answer to a gain check is a message that satisfies its own rules, whatever the plans and the
// share, and it says what the domain check says.
func TestAGainCheckIsAMessageThatHoldsItsRules(t *testing.T) {
	r := newRand(31)
	for range 2000 {
		plans := anyGainPlans(r.IntN(14), func(int) *core.GainBound {
			if r.IntN(3) == 0 {
				return nil
			}
			return &core.GainBound{Share: float64(r.IntN(40)) / 100, RelativeTo: core.GainHeadline}
		})
		share := float64(r.IntN(40)) / 100
		got, err := wire.CheckGain(wire.Plans(plans), share)
		if err != nil {
			t.Fatal(err)
		}
		if err := wire.Validate(got); err != nil {
			t.Fatalf("share %v over %+v: %v in %v", share, plans, err, got)
		}
		want := core.CheckGain(plans, share)
		if (got.GetAnswer() == cliv1.PlanGainAnswer_PLAN_GAIN_ANSWER_ALL_UNDER) != want.AllUnder() || int(got.GetCounted()) != want.Counted || len(got.GetBlockers()) != len(want.Blockers) {
			t.Fatalf("share %v: wire says %v, domain says %+v", share, got, want)
		}
	}
}

func TestAPlanWithABoundSurvivesTheContract(t *testing.T) {
	r := newRand(32)
	for range 500 {
		plans := anyGainPlans(3, func(int) *core.GainBound {
			return &core.GainBound{Share: r.Float64() * 5, RelativeTo: core.GainHeadline}
		})
		for _, p := range plans {
			again, err := wire.ToPlan(wire.Plan(p))
			if err != nil || again.GainBound == nil || *again.GainBound != *p.GainBound {
				t.Fatalf("%+v came back as %+v, %v", p.GainBound, again.GainBound, err)
			}
		}
	}
}

// A bound that is not a finite share, or of nothing there is, is refused naming the field.
func TestABoundThatIsNotAShareOfAReferenceIsRefusedNamingTheField(t *testing.T) {
	headline := cliv1.PlanGainReference_PLAN_GAIN_REFERENCE_HEADLINE
	for _, b := range []*cliv1.PlanGainBound{
		{Share: -0.1, RelativeTo: headline},
		{Share: math.NaN(), RelativeTo: headline},
		{Share: math.Inf(1), RelativeTo: headline},
		{Share: 0.1},
		{Share: 0.1, RelativeTo: 77},
	} {
		if _, err := wire.ToGainBound("plan.gain_bound", b); err == nil {
			t.Errorf("%v was accepted", b)
		}
		if err := wire.Validate(&cliv1.CreatePlanRequest{Title: "t", GainBound: b}); err == nil {
			t.Errorf("a request with %v passed its rules", b)
		}
	}
}
