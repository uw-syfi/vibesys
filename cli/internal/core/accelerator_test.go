package core_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func TestParseAcceleratorUsesOnlyWholeModelTokens(t *testing.T) {
	for _, c := range []struct {
		vendor      core.AcceleratorVendor
		name, count string
		model       core.AcceleratorModel
		invalid     bool
	}{
		{core.VendorNVIDIA, "NVIDIA H100 80GB HBM3", "8", core.ModelH100, false},
		{core.VendorNVIDIA, "NVIDIA L40", "1", core.ModelOther, false},
		{core.VendorNVIDIA, "NVIDIA L4", "1", core.ModelL4, false},
		{core.VendorAMD, "AMD Instinct MI300X", "2", core.ModelMI300X, false},
		{core.VendorAMD, "NVIDIA H100", "2", core.ModelOther, false},
		{core.VendorAMD, "unread", "unread", "", true},
		{core.VendorAMD, "MI210", "0", "", true},
		{core.VendorAMD, "MI210", "4097", "", true},
		{core.VendorAMD, "", "1", "", true},
	} {
		got, err := core.ParseAccelerator(c.vendor, c.name, c.count)
		if c.invalid {
			if err == nil {
				t.Errorf("%q %q: expected error", c.name, c.count)
			}
			continue
		}
		if err != nil || got.Model != c.model || got.Vendor != c.vendor || got.DeviceName != c.name {
			t.Errorf("%q: %v, %v", c.name, got, err)
		}
	}
}
