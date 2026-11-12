package task_test

import (
	"testing"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// A need or an offer names a model only of its own vendor: the pairs are checked by the contract, not
// by whoever reads them. A need may name no model (any of the vendor); an offer always names one, and
// a CPU need names none.
func TestAnAcceleratorModelMustBelongToItsVendor(t *testing.T) {
	vendors, models := cliv1.AcceleratorVendor_name, cliv1.AcceleratorModel_name
	for vendor := range vendors {
		if vendor == 0 {
			continue
		}
		for model := range models {
			legal := legalPair(cliv1.AcceleratorVendor(vendor), cliv1.AcceleratorModel(model))
			need := &cliv1.AcceleratorNeed{Vendor: cliv1.AcceleratorVendor(vendor), Model: cliv1.AcceleratorModel(model), Count: 1}
			if err := protovalidate.Validate(need); (err == nil) != legal {
				t.Errorf("need %v %v: valid=%v, want %v", need.Vendor, need.Model, err == nil, legal)
			}
			offer := &cliv1.AcceleratorOffer{Vendor: need.Vendor, Model: need.Model, Count: 1}
			if err := protovalidate.Validate(offer); (err == nil) != (legal && model != 0) {
				t.Errorf("offer %v %v: valid=%v, want %v", offer.Vendor, offer.Model, err == nil, legal && model != 0)
			}
		}
	}
}

func legalPair(vendor cliv1.AcceleratorVendor, model cliv1.AcceleratorModel) bool {
	if model == cliv1.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED {
		return true
	}
	if vendor == cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_CPU {
		return false
	}
	owner := map[cliv1.AcceleratorModel]cliv1.AcceleratorVendor{
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI210:         cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD,
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI250:         cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD,
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300A:        cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD,
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X:        cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD,
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI325X:        cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD,
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100:          cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA,
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_B200:          cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA,
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_L4:            cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA,
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_A100:          cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA,
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_TRAINIUM2:     cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AWS,
		cliv1.AcceleratorModel_ACCELERATOR_MODEL_APPLE_SILICON: cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_APPLE,
	}
	if model == cliv1.AcceleratorModel_ACCELERATOR_MODEL_OTHER {
		return true
	}
	return owner[model] == vendor
}
