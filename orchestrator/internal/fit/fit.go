// Package fit decides whether a task can run on the host: whether this build's job tool can run it at
// all, and whether the hardware it needs is given by an eval place. It is pure: the same card and
// places always give the same answer, and nothing is read from outside.
//
// The rule is written once here and states the precedence of the card's own word and of
// AcceleratorNeed and FitState:
//
//  0. A task whose card says the job tool cannot run it (TaskCard.unsupported) fits nowhere, whatever
//     the places are: no place can give what no job can use. The word is the job tool's own, from the
//     loader every job reads the task through, so this package holds no list of what is supported.
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

// Of is the fit of a task, by its card, over the places the host can evaluate on: the state, and the
// names of the places that give the card's need, in the order of places, when the state is FITS.
//
//   - A card that says the job tool cannot run the task: UNSUPPORTED, with no place, whatever the places.
//   - No place has a known state (none at all, or every one unreadable): PLACES_UNREADABLE.
//   - A need whose model is OTHER, which names an accelerator the host has no entry for: UNKNOWN_ACCELERATOR.
//   - Otherwise FITS when some place gives the need, else NO_ACCELERATOR.
func Of(card *cli.TaskCard, places []*pb.EvalPlace) *pb.Fit {
	if card.GetUnsupported() != "" {
		return &pb.Fit{State: pb.FitState_FIT_STATE_UNSUPPORTED}
	}
	need := card.GetHardware()
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
