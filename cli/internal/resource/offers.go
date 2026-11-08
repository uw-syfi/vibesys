package resource

import (
	"errors"
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

// ErrNoDeclaredAccelerators says a placement declares no accelerator resource, so there is nothing to report.
var ErrNoDeclaredAccelerators = errors.New("no declared accelerators")

// DeclaredOffers reads what an operator config declares a placement gives, for a kind the daemon
// cannot probe: the vendor comes from the accelerator resource ([resources.gpu] is NVIDIA,
// [resources.rocm] is AMD) and the models and counts from the placement's gres, such as
// "gpu:h100:8" or "gpu:4" (a gres with no model names the model OTHER).
// Without an accelerator resource it fails with ErrNoDeclaredAccelerators.
// This reading is temporary until the shared accelerator identity registry replaces it.
func DeclaredOffers(slurm *cliv1.SlurmSettings, res *cliv1.Resources) ([]*cliv1.AcceleratorOffer, error) {
	var vendor cliv1.AcceleratorVendor
	switch {
	case res.GetGpu() != nil && res.GetRocm() != nil:
		return nil, errors.New("both a gpu and a rocm resource are declared: the accelerators cannot be told apart")
	case res.GetGpu() != nil:
		vendor = cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA
	case res.GetRocm() != nil:
		vendor = cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD
	default:
		return nil, ErrNoDeclaredAccelerators
	}
	var offers []*cliv1.AcceleratorOffer
	for item := range strings.SplitSeq(slurm.GetGres(), ",") {
		item, _, _ = strings.Cut(item, "(")
		parts := strings.Split(item, ":")
		if parts[0] != "gpu" || len(parts) < 2 || len(parts) > 3 {
			continue
		}
		name := "gpu"
		if len(parts) == 3 {
			name = parts[1]
		}
		offer, err := AcceleratorOffer(vendor, name, parts[len(parts)-1])
		if err != nil {
			return nil, fmt.Errorf("gres %q: %w", item, err)
		}
		if len(parts) == 2 {
			offer.DeviceName = ""
		}
		offers = append(offers, offer)
	}
	if len(offers) == 0 {
		return nil, fmt.Errorf("the declared accelerator resource has no gpu count in gres %q", slurm.GetGres())
	}
	return offers, nil
}
