package production

import (
	"errors"
	"fmt"
	"strconv"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// errNoDeclaredAccelerators says a placement declares no accelerator resource, so there is nothing
// to report.
var errNoDeclaredAccelerators = errors.New("no declared accelerators")

// placementProbe is how the daemon learns what a placement gives. A Slurm or Apptainer placement is
// declared in the operator config: its resources read the login node, not the compute nodes the
// jobs get, and a probe would have to hold an allocation. Any other placement is probed through its
// own resources.
func placementProbe(name string, pl *cliv1.Placement, provider *docker.Provider) (app.Placement, error) {
	kind, err := wire.ToPlacementKind("kind", pl.GetKind())
	if err != nil {
		return app.Placement{}, err
	}
	out := app.Placement{Kind: kind, MaxJobs: pl.GetMaxJobs(), Probe: provider, Source: core.OfferProbed}
	if kind != core.PlacementSlurm && kind != core.PlacementApptainer {
		return out, nil
	}
	pool, err := operator.PoolOf(name, pl)
	if err != nil {
		return app.Placement{}, err
	}
	offers, err := declaredOffers(pool)
	if errors.Is(err, errNoDeclaredAccelerators) {
		err = fmt.Errorf("%w: %w", app.ErrNotProbeable, err)
	}
	out.Probe, out.Source = app.DeclaredOffers{Declared: offers, Err: err}, core.OfferDeclared
	return out, nil
}

// declaredOffers reads what an operator config declares a cluster placement gives: the vendor is
// the GPU table's ([resources.gpu] is NVIDIA, [resources.rocm] is AMD), the count its count, and the
// model its gres_type, such as h100 (no gres_type names the model OTHER). A gres_type names its
// model through the accelerator registry (core.ParseAccelerator). Without a pool it fails with
// errNoDeclaredAccelerators.
func declaredOffers(pool operator.Pool) ([]core.Accelerator, error) {
	if !pool.Size.IsSet() {
		return nil, errNoDeclaredAccelerators
	}
	name := "gpu"
	if pool.GresType != "" {
		name = pool.GresType
	}
	offer, err := core.ParseAccelerator(pool.Vendor, name, strconv.Itoa(pool.Size.Int()))
	if err != nil {
		return nil, fmt.Errorf("%s: %w", pool.Key, err)
	}
	if pool.GresType == "" {
		offer.DeviceName = ""
	}
	return []core.Accelerator{offer}, nil
}
