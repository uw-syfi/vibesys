package core_test

import (
	"errors"
	"math"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// anyPlans is plans of random statuses, some with a bound and some without, with numbers in no order.
func anyPlans(r *rand.Rand) []core.Plan {
	var plans []core.Plan
	for i := range r.IntN(12) {
		p := core.Plan{Number: core.PlanNumber(100 - i), Status: core.PlanStatuses()[r.IntN(5)]}
		if r.IntN(3) > 0 {
			p.GainBound = &core.GainBound{Share: float64(r.IntN(30)) / 100, RelativeTo: core.GainHeadline}
		}
		plans = append(plans, p)
	}
	return plans
}

// Whatever the plans, the check is yes exactly when each open or parked plan states a bound below
// the share; the plans that stop it are those, by number; and a plan in another status is never asked.
func TestAGainCheckIsYesExactlyWhenEveryOpenOrParkedPlanIsUnderTheShare(t *testing.T) {
	r := newRand(21)
	for range properties {
		plans := anyPlans(r)
		share := float64(r.IntN(30)) / 100
		got := core.CheckGain(plans, share)

		var counted int
		var want []core.PlanNumber
		for _, p := range plans {
			if p.Status != core.PlanOpen && p.Status != core.PlanParked {
				continue
			}
			counted++
			if p.GainBound == nil || p.GainBound.Share >= share {
				want = append(want, p.Number)
			}
		}
		slices.Sort(want)
		var blockers []core.PlanNumber
		for _, p := range got.Blockers {
			blockers = append(blockers, p.Number)
		}
		if got.Counted != counted || !slices.Equal(blockers, want) || got.AllUnder() != (len(want) == 0) || got.Share != share {
			t.Fatalf("share %v over %+v: got %+v, want %d counted and blockers %v", share, plans, got, counted, want)
		}
	}
}

// Raising the share can only clear blockers, and a plan that states no bound blocks at every share.
func TestAHigherShareNeverAddsABlockerAndAnUnstatedBoundAlwaysBlocks(t *testing.T) {
	r := newRand(22)
	for range properties {
		plans := anyPlans(r)
		low := float64(r.IntN(30)) / 100
		high := low + float64(r.IntN(30))/100
		lowCheck, highCheck := core.CheckGain(plans, low), core.CheckGain(plans, high)
		inLow := map[core.PlanNumber]bool{}
		for _, p := range lowCheck.Blockers {
			inLow[p.Number] = true
		}
		for _, p := range highCheck.Blockers {
			if !inLow[p.Number] {
				t.Fatalf("plan %d blocks at %v and not at %v", p.Number, high, low)
			}
		}
		huge := core.CheckGain(plans, 1e9)
		for _, p := range plans {
			if p.GainBound == nil && (p.Status == core.PlanOpen || p.Status == core.PlanParked) {
				if !slices.ContainsFunc(huge.Blockers, func(b core.Plan) bool { return b.Number == p.Number }) {
					t.Fatalf("plan %d states no bound and does not block at any share", p.Number)
				}
			}
		}
	}
}

func TestOnlyAFiniteShareOfAKnownReferenceIsABound(t *testing.T) {
	for _, share := range []float64{0, 0.05, 1, 40} {
		if err := core.CheckGainBound("b", core.GainBound{Share: share, RelativeTo: core.GainHeadline}); err != nil {
			t.Errorf("share %v: %v", share, err)
		}
	}
	for _, share := range []float64{-0.01, math.NaN(), math.Inf(1), math.Inf(-1)} {
		err := core.CheckGainBound("b", core.GainBound{Share: share, RelativeTo: core.GainHeadline})
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "b:") {
			t.Errorf("share %v: %v, want invalid input naming the field", share, err)
		}
	}
	if err := core.CheckGainBound("b", core.GainBound{Share: 0.1, RelativeTo: "base"}); err == nil {
		t.Error("an unknown reference was accepted")
	}
}
