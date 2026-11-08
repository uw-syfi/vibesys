package wire

import (
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

var placementKinds = []struct {
	domain core.PlacementKind
	wire   cliv1.PlacementKind
}{
	{core.PlacementLocal, cliv1.PlacementKind_PLACEMENT_KIND_LOCAL},
	{core.PlacementSSH, cliv1.PlacementKind_PLACEMENT_KIND_SSH},
	{core.PlacementSlurm, cliv1.PlacementKind_PLACEMENT_KIND_SLURM},
	{core.PlacementApptainer, cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER},
}

// ToPlacementKind returns the domain kind for the contract's value, or invalid_input naming field.
func ToPlacementKind(field string, k cliv1.PlacementKind) (core.PlacementKind, error) {
	for _, p := range placementKinds {
		if p.wire == k {
			return p.domain, nil
		}
	}
	return 0, invalid("%s: %s is not a placement kind", field, k)
}

var reachStates = map[core.Reach]cliv1.ReachState{
	core.ReachReachable:   cliv1.ReachState_REACH_STATE_REACHABLE,
	core.ReachUnreachable: cliv1.ReachState_REACH_STATE_UNREACHABLE,
	core.ReachNotProbed:   cliv1.ReachState_REACH_STATE_NOT_PROBED,
}

// AcceleratorOffer returns the contract's offer for an accelerator, or unspecified vendor and
// model for values it does not name: the contract's validation then refuses the message.
func AcceleratorOffer(a core.Accelerator) *cliv1.AcceleratorOffer {
	vendor, model := task.WireAccelerator(a.Vendor, a.Model)
	return &cliv1.AcceleratorOffer{Vendor: vendor, Model: model, Count: a.Count, DeviceName: a.DeviceName}
}

// PlacementView returns the contract's view of a placement reading.
func PlacementView(r core.PlacementReading) *cliv1.PlacementView {
	view := &cliv1.PlacementView{Name: r.Name, MaxJobs: r.MaxJobs, Reach: &cliv1.PlacementReach{State: reachStates[r.Reach], Reason: r.Reason}}
	for _, p := range placementKinds {
		if p.domain == r.Kind {
			view.Kind = p.wire
		}
	}
	if r.Unknown != core.OffersKnown {
		reason := cliv1.OffersUnknownReason_OFFERS_UNKNOWN_REASON_UNREADABLE
		switch r.Unknown {
		case core.OffersTimedOut:
			reason = cliv1.OffersUnknownReason_OFFERS_UNKNOWN_REASON_TIMED_OUT
		case core.OffersNotProbeable:
			reason = cliv1.OffersUnknownReason_OFFERS_UNKNOWN_REASON_NOT_PROBEABLE
		}
		view.Offers = &cliv1.PlacementView_Unknown{Unknown: &cliv1.OffersUnknown{Reason: reason, Detail: r.Detail}}
		return view
	}
	known := &cliv1.PlacementOffers{Source: cliv1.OfferSource_OFFER_SOURCE_PROBED}
	if r.Source == core.OfferDeclared {
		known.Source = cliv1.OfferSource_OFFER_SOURCE_DECLARED
	}
	if !r.ProbedAt.IsZero() {
		known.ProbedAt = timestamppb.New(r.ProbedAt)
	}
	for _, a := range r.Offers {
		known.Offers = append(known.Offers, AcceleratorOffer(a))
	}
	view.Offers = &cliv1.PlacementView_Known{Known: known}
	return view
}
