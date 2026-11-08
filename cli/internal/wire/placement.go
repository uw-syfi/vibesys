package wire

import (
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
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

var acceleratorVendors = []struct {
	domain core.AcceleratorVendor
	wire   cliv1.AcceleratorVendor
}{
	{core.VendorNVIDIA, cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA},
	{core.VendorAMD, cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD},
}

var acceleratorModels = []struct {
	domain core.AcceleratorModel
	wire   cliv1.AcceleratorModel
}{
	{core.ModelOther, cliv1.AcceleratorModel_ACCELERATOR_MODEL_OTHER},
	{core.ModelH100, cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100},
	{core.ModelB200, cliv1.AcceleratorModel_ACCELERATOR_MODEL_B200},
	{core.ModelL4, cliv1.AcceleratorModel_ACCELERATOR_MODEL_L4},
	{core.ModelA100, cliv1.AcceleratorModel_ACCELERATOR_MODEL_A100},
	{core.ModelMI210, cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI210},
	{core.ModelMI300A, cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300A},
	{core.ModelMI300X, cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X},
	{core.ModelMI325X, cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI325X},
}

// AcceleratorOffer returns the contract's offer for an accelerator, or unspecified vendor and
// model for values it does not name: the contract's validation then refuses the message.
func AcceleratorOffer(a core.Accelerator) *cliv1.AcceleratorOffer {
	out := &cliv1.AcceleratorOffer{Count: a.Count, DeviceName: a.DeviceName}
	for _, p := range acceleratorVendors {
		if p.domain == a.Vendor {
			out.Vendor = p.wire
		}
	}
	for _, p := range acceleratorModels {
		if p.domain == a.Model {
			out.Model = p.wire
		}
	}
	return out
}

// PlacementView returns the contract's view of a placement reading.
func PlacementView(r core.PlacementReading) *cliv1.PlacementView {
	view := &cliv1.PlacementView{Name: r.Name, MaxJobs: r.MaxJobs, Reach: &cliv1.PlacementReach{Reachable: r.Reachable, Reason: r.Reason}}
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
