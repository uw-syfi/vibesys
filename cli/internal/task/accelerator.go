package task

import (
	"slices"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// Accelerator is what a display name of a device names: its vendor and, unless the name is of a
// whole vendor ("NVIDIA GPU") or a CPU, its model.
type Accelerator struct {
	Vendor cliv1.AcceleratorVendor
	// Model is unspecified for a name that stands for any device of the vendor, and for a CPU.
	Model cliv1.AcceleratorModel
}

// accelerators is the registry: the one list of the display names a task may author and what each
// names. It is closed. A name that is not in it is an error, not a model of OTHER, so a typo in a
// task is found where the task is read and no task is quietly given a device that fits nothing. A
// new accelerator is a new row here and a new value of cliv1.AcceleratorModel, together.
//
// It is new: before it, hardware was only the display string a task authored. It is seeded with the
// names the repository's examples author, and the models the contract defines. A name is matched
// exactly, as authored.
var accelerators = map[string]Accelerator{
	"CPU":           {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_CPU, cliv1.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED},
	"AMD MI210":     {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI210},
	"AMD MI300A":    {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300A},
	"AMD MI300X":    {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X},
	"AMD MI325X":    {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI325X},
	"NVIDIA GPU":    {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cliv1.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED},
	"NVIDIA H100":   {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100},
	"NVIDIA B200":   {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cliv1.AcceleratorModel_ACCELERATOR_MODEL_B200},
	"NVIDIA L4":     {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cliv1.AcceleratorModel_ACCELERATOR_MODEL_L4},
	"NVIDIA A100":   {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cliv1.AcceleratorModel_ACCELERATOR_MODEL_A100},
	"AWS Trainium2": {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AWS, cliv1.AcceleratorModel_ACCELERATOR_MODEL_TRAINIUM2},
	"Apple Silicon": {cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_APPLE, cliv1.AcceleratorModel_ACCELERATOR_MODEL_APPLE_SILICON},
}

// LookupAccelerator returns what a display name names, and false for a name the registry does not
// have.
func LookupAccelerator(name string) (Accelerator, bool) {
	a, ok := accelerators[name]
	return a, ok
}

// AcceleratorNames lists the registry's display names, sorted.
func AcceleratorNames() []string {
	names := make([]string, 0, len(accelerators))
	for n := range accelerators {
		names = append(names, n)
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
