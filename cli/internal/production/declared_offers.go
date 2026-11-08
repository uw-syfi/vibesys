package production

import (
	"errors"
	"fmt"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// errNoDeclaredAccelerators says a placement declares no accelerator resource, so there is nothing
// to report.
var errNoDeclaredAccelerators = errors.New("no declared accelerators")

// placementProbe is how the daemon learns what a placement gives. A Slurm or Apptainer placement is
// declared in the operator config: its resources read the login node, not the compute nodes the
// jobs get, and a probe would have to hold an allocation. Any other placement is probed through its
// own resources.
func placementProbe(pl *cliv1.Placement, provider *docker.Provider) (app.Placement, error) {
	kind, err := wire.ToPlacementKind("kind", pl.GetKind())
	if err != nil {
		return app.Placement{}, err
	}
	out := app.Placement{Kind: kind, MaxJobs: pl.GetMaxJobs(), Probe: provider, Source: core.OfferProbed}
	if kind != core.PlacementSlurm && kind != core.PlacementApptainer {
		return out, nil
	}
	offers, err := declaredOffers(pl.GetSlurm(), pl.GetResources())
	if errors.Is(err, errNoDeclaredAccelerators) {
		err = fmt.Errorf("%w: %w", app.ErrNotProbeable, err)
	}
	out.Probe, out.Source = app.DeclaredOffers{Declared: offers, Err: err}, core.OfferDeclared
	return out, nil
}

// declaredOffers reads what an operator config declares a placement gives: the vendor comes from
// the accelerator resource ([resources.gpu] is NVIDIA, [resources.rocm] is AMD) and the models and
// counts from the placement's gres, such as "gpu:h100:8" or "gpu:4" (a gres with no model names the
// model OTHER). Without an accelerator resource it fails with errNoDeclaredAccelerators.
// This reading is temporary until the shared accelerator identity registry replaces it.
func declaredOffers(slurm *cliv1.SlurmSettings, res *cliv1.Resources) ([]core.Accelerator, error) {
	var vendor core.AcceleratorVendor
	switch {
	case res.GetGpu() != nil && res.GetRocm() != nil:
		return nil, errors.New("both a gpu and a rocm resource are declared: the accelerators cannot be told apart")
	case res.GetGpu() != nil:
		vendor = core.VendorNVIDIA
	case res.GetRocm() != nil:
		vendor = core.VendorAMD
	default:
		return nil, errNoDeclaredAccelerators
	}
	var offers []core.Accelerator
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
		offer, err := core.ParseAccelerator(vendor, name, parts[len(parts)-1])
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
