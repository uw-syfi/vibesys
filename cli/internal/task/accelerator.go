package task

import (
	"slices"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Accelerator is what a display name of a device names: its vendor and, unless the name is of a
// whole vendor ("NVIDIA GPU") or a CPU, its model.
type Accelerator struct {
	Vendor cliv1.AcceleratorVendor
	// Model is unspecified for a name that stands for any device of the vendor, and for a CPU.
	Model cliv1.AcceleratorModel
}

var wireVendors = map[core.AcceleratorVendor]cliv1.AcceleratorVendor{
	core.VendorNVIDIA: cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA,
	core.VendorAMD:    cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD,
	core.VendorAWS:    cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AWS,
	core.VendorApple:  cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_APPLE,
	core.VendorCPU:    cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_CPU,
}

var wireModels = map[core.AcceleratorModel]cliv1.AcceleratorModel{
	"":                     cliv1.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED,
	core.ModelOther:        cliv1.AcceleratorModel_ACCELERATOR_MODEL_OTHER,
	core.ModelH100:         cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100,
	core.ModelB200:         cliv1.AcceleratorModel_ACCELERATOR_MODEL_B200,
	core.ModelL4:           cliv1.AcceleratorModel_ACCELERATOR_MODEL_L4,
	core.ModelA100:         cliv1.AcceleratorModel_ACCELERATOR_MODEL_A100,
	core.ModelMI210:        cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI210,
	core.ModelMI250:        cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI250,
	core.ModelMI300A:       cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300A,
	core.ModelMI300X:       cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X,
	core.ModelMI325X:       cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI325X,
	core.ModelTrainium2:    cliv1.AcceleratorModel_ACCELERATOR_MODEL_TRAINIUM2,
	core.ModelAppleSilicon: cliv1.AcceleratorModel_ACCELERATOR_MODEL_APPLE_SILICON,
}

// WireAccelerator is the contract's vendor and model of a domain vendor and model, the one mapping
// between the two. A value the contract does not name is unspecified, which its validation refuses.
func WireAccelerator(vendor core.AcceleratorVendor, model core.AcceleratorModel) (cliv1.AcceleratorVendor, cliv1.AcceleratorModel) {
	return wireVendors[vendor], wireModels[model]
}

// LookupAccelerator returns what a display name names, and false for a name the accelerator registry
// (core.Accelerators) does not have. The registry is closed: a name that is not in it is an error, not
// a model of OTHER, so a typo in a task is found where the task is read and no task is quietly given a
// device that fits nothing.
func LookupAccelerator(name string) (Accelerator, bool) {
	row, ok := core.LookupAccelerator(name)
	if !ok {
		return Accelerator{}, false
	}
	vendor, model := WireAccelerator(row.Vendor, row.Model)
	return Accelerator{Vendor: vendor, Model: model}, true
}

// AcceleratorNames lists the registry's display names, sorted.
func AcceleratorNames() []string {
	var names []string
	for _, a := range core.Accelerators() {
		names = append(names, a.Display)
	}
	slices.Sort(names)
	return names
}

// cardHardware is the need a [catalogue.hardware] table states. The name must be in the registry
// and the vendor the table authors must be the name's: a disagreement is an error naming the key,
// since either would be a wrong place to run the task.
func cardHardware(h *cliv1.TaskHardwareV02) (*cliv1.AcceleratorNeed, error) {
	a, ok := LookupAccelerator(h.GetAccelerator())
	if !ok {
		return nil, invalid("%s [catalogue.hardware]: accelerator %q is not a known accelerator (known: %s)",
			ManifestName, h.GetAccelerator(), strings.Join(AcceleratorNames(), ", "))
	}
	if a.Vendor != h.GetVendor() {
		return nil, invalid("%s [catalogue.hardware]: vendor %q does not match accelerator %q, a %q device",
			ManifestName, vendorWord(h.GetVendor()), h.GetAccelerator(), vendorWord(a.Vendor))
	}
	return &cliv1.AcceleratorNeed{Vendor: a.Vendor, Model: a.Model, Count: h.GetCount()}, nil
}

func vendorWord(v cliv1.AcceleratorVendor) string {
	return strings.ToLower(strings.TrimPrefix(v.String(), "ACCELERATOR_VENDOR_"))
}
