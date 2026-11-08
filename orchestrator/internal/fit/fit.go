// Package fit decides whether the hardware a task needs is given by an eval place. It is pure: the
// same need and places always give the same answer, and nothing is read from outside.
//
// The rule is written once here and states the precedence of AcceleratorNeed and FitState:
//
//  1. A place whose hardware is unknown or unreadable fits nothing, a CPU need included.
//  2. A CPU need, or a task that states no hardware, fits every place with a known state.
//  3. An offer whose model is OTHER fits nothing: a device with no registry entry cannot be shown to be
//     the one wanted, not even by a need that names only a vendor.
//  4. Otherwise an offer fits when the vendor is the same, its count is at least the need's, and the
//     model is the same or the need names none ("NVIDIA GPU" fits any known device of NVIDIA).
//
// Offers are judged one by one: the count of an offer is not added to another offer of the same model.
package fit

import (
	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Fits reports whether one place gives the need. A nil need is a task that states no hardware.
func Fits(need *cli.AcceleratorNeed, place *pb.EvalPlace) bool {
	if place.GetKnown() == nil {
		return false
	}
	if need == nil || need.GetVendor() == cli.AcceleratorVendor_ACCELERATOR_VENDOR_CPU {
		return true
	}
	for _, offer := range place.GetOffers() {
		if offerFits(need, offer) {
			return true
		}
	}
	return false
}

func offerFits(need *cli.AcceleratorNeed, offer *pb.EvalOffer) bool {
	if offer.GetModel() == cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER {
		return false
	}
	if need.GetVendor() == cli.AcceleratorVendor_ACCELERATOR_VENDOR_UNSPECIFIED || offer.GetVendor() != need.GetVendor() {
		return false
	}
	if offer.GetCount() < need.GetCount() {
		return false
	}
	return need.GetModel() == cli.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED || offer.GetModel() == need.GetModel()
}

// Of is the fit of a need over the places the host can evaluate on: the state, and the names of the
// places that give the need, in the order of places, when the state is FITS.
//
//   - No place has a known state (none at all, or every one unreadable): PLACES_UNREADABLE.
//   - A need whose model is OTHER, which names an accelerator the host has no entry for: UNKNOWN_ACCELERATOR.
//   - Otherwise FITS when some place gives the need, else NO_ACCELERATOR.
func Of(need *cli.AcceleratorNeed, places []*pb.EvalPlace) *pb.Fit {
	readable := false
	for _, place := range places {
		if place.GetKnown() != nil {
			readable = true
		}
	}
	if !readable {
		return &pb.Fit{State: pb.FitState_FIT_STATE_PLACES_UNREADABLE}
	}
	if need.GetModel() == cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER {
		return &pb.Fit{State: pb.FitState_FIT_STATE_UNKNOWN_ACCELERATOR}
	}
	var names []string
	for _, place := range places {
		if Fits(need, place) {
			names = append(names, place.GetName())
		}
	}
	if len(names) == 0 {
		return &pb.Fit{State: pb.FitState_FIT_STATE_NO_ACCELERATOR}
	}
	return &pb.Fit{State: pb.FitState_FIT_STATE_FITS, Places: names}
}
