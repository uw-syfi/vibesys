package resource

import (
	"fmt"
	"strconv"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// AcceleratorOffer converts the facts used in an environment fingerprint into an offer.
// This seed is temporary until the shared accelerator identity registry is available.
func AcceleratorOffer(vendor cliv1.AcceleratorVendor, name, count string) (*cliv1.AcceleratorOffer, error) {
	n, err := strconv.ParseUint(count, 10, 32)
	if err != nil || n == 0 || n > 4096 || name == "" || len(name) > 128 {
		return nil, fmt.Errorf("unreadable accelerator model %q or count %q", name, count)
	}
	model := cliv1.AcceleratorModel_ACCELERATOR_MODEL_OTHER
	// Match tokens rather than substrings: e.g. L40 must not be reported as L4.
	words := strings.FieldsFunc(strings.ToUpper(name), func(r rune) bool { return !(r >= 'A' && r <= 'Z' || r >= '0' && r <= '9') })
	models := map[cliv1.AcceleratorVendor]map[string]cliv1.AcceleratorModel{
		cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA: {"H100": cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100, "B200": cliv1.AcceleratorModel_ACCELERATOR_MODEL_B200, "L4": cliv1.AcceleratorModel_ACCELERATOR_MODEL_L4, "A100": cliv1.AcceleratorModel_ACCELERATOR_MODEL_A100},
		cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD:    {"MI210": cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI210, "MI300A": cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300A, "MI300X": cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X, "MI325X": cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI325X},
	}
	for _, word := range words {
		if m, ok := models[vendor][word]; ok {
			model = m
			break
		}
	}
	return &cliv1.AcceleratorOffer{Vendor: vendor, Model: model, Count: uint32(n), DeviceName: name}, nil
}
