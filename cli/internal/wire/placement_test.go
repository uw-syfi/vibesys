package wire_test

import (
	"fmt"
	"math/rand/v2"
	"testing"
	"time"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

var wantReach = map[core.Reach]cliv1.ReachState{
	core.ReachReachable:   cliv1.ReachState_REACH_STATE_REACHABLE,
	core.ReachUnreachable: cliv1.ReachState_REACH_STATE_UNREACHABLE,
	core.ReachNotProbed:   cliv1.ReachState_REACH_STATE_NOT_PROBED,
}

// Every placement reading the daemon can make is a valid contract message, and the view says what
// the reading says.
func TestPlacementViewsOfAnyReadingAreValid(t *testing.T) {
	kinds := []core.PlacementKind{core.PlacementLocal, core.PlacementSSH, core.PlacementSlurm, core.PlacementApptainer}
	unknowns := []core.OffersUnknownReason{core.OffersKnown, core.OffersUnreadable, core.OffersTimedOut, core.OffersNotProbeable}
	models := map[core.AcceleratorVendor][]core.AcceleratorModel{
		core.VendorNVIDIA: {core.ModelOther, core.ModelH100, core.ModelB200, core.ModelL4, core.ModelA100},
		core.VendorAMD:    {core.ModelOther, core.ModelMI210, core.ModelMI250, core.ModelMI300A, core.ModelMI300X, core.ModelMI325X},
	}
	vendors := []core.AcceleratorVendor{core.VendorNVIDIA, core.VendorAMD}
	reaches := []core.Reach{core.ReachReachable, core.ReachUnreachable, core.ReachNotProbed}
	rng := rand.New(rand.NewPCG(3, 4))
	for range 500 {
		r := core.PlacementReading{
			Name: fmt.Sprintf("place-%d", rng.IntN(100)), Kind: kinds[rng.IntN(len(kinds))], MaxJobs: uint32(1 + rng.IntN(8)),
			Unknown: unknowns[rng.IntN(len(unknowns))], Reach: reaches[rng.IntN(len(reaches))],
		}
		if r.Reach == core.ReachUnreachable {
			r.Reason = "place: unreachable"
		}
		if r.Unknown != core.OffersKnown {
			r.Detail = "place: why"
		} else {
			r.Source = core.OfferProbed
			if rng.IntN(2) == 0 {
				r.Source = core.OfferDeclared
			} else {
				r.ProbedAt = time.Unix(int64(1+rng.IntN(1000)), 0)
			}
			for range rng.IntN(4) {
				v := vendors[rng.IntN(len(vendors))]
				ms := models[v]
				r.Offers = append(r.Offers, core.Accelerator{Vendor: v, Model: ms[rng.IntN(len(ms))], Count: uint32(1 + rng.IntN(8))})
			}
		}
		view := wire.PlacementView(r)
		if err := protovalidate.Validate(view); err != nil {
			t.Fatalf("%+v: %v", r, err)
		}
		if view.GetName() != r.Name || view.GetMaxJobs() != r.MaxJobs || view.GetReach().GetState() != wantReach[r.Reach] ||
			(view.GetKnown() != nil) != (r.Unknown == core.OffersKnown) || len(view.GetKnown().GetOffers()) != len(r.Offers) ||
			(view.GetKnown().GetProbedAt() != nil) != !r.ProbedAt.IsZero() {
			t.Fatalf("%+v became %v", r, view)
		}
	}
}

func TestPlacementKindsRoundTripAndUnspecifiedIsRefused(t *testing.T) {
	for _, k := range []cliv1.PlacementKind{cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, cliv1.PlacementKind_PLACEMENT_KIND_SSH, cliv1.PlacementKind_PLACEMENT_KIND_SLURM, cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER} {
		d, err := wire.ToPlacementKind("kind", k)
		if err != nil || wire.PlacementView(core.PlacementReading{Kind: d}).GetKind() != k {
			t.Errorf("%v: %v, %v", k, d, err)
		}
	}
	if _, err := wire.ToPlacementKind("kind", cliv1.PlacementKind_PLACEMENT_KIND_UNSPECIFIED); err == nil {
		t.Error("unspecified accepted")
	}
}
