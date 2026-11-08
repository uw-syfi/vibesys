package resource_test

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"testing"
)

func TestAcceleratorOffersUseOnlyWholeModelTokens(t *testing.T) {
	for _, c := range []struct {
		vendor      cliv1.AcceleratorVendor
		name, count string
		model       cliv1.AcceleratorModel
		invalid     bool
	}{
		{cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, "NVIDIA H100 80GB HBM3", "8", cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100, false},
		{cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, "NVIDIA L40", "1", cliv1.AcceleratorModel_ACCELERATOR_MODEL_OTHER, false},
		{cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, "AMD Instinct MI300X", "2", cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X, false},
		{cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, "unread", "unread", 0, true},
		{cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, "MI210", "0", 0, true},
	} {
		got, err := resource.AcceleratorOffer(c.vendor, c.name, c.count)
		if c.invalid {
			if err == nil {
				t.Errorf("%q: expected error", c.name)
			}
			continue
		}
		if err != nil || got.GetModel() != c.model || got.GetVendor() != c.vendor || got.GetDeviceName() != c.name {
			t.Errorf("%q: %v, %v", c.name, got, err)
		}
	}
}
