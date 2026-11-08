package production

import (
	"errors"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
)

// test-isolation: placementProbe is wiring that RunDaemon reaches only with a real runtime directory.

// A Slurm or Apptainer placement reports what its config declares and never probes the login node;
// every other kind is probed through its own resources.
func TestOnlyClusterPlacementsReportDeclaredOffers(t *testing.T) {
	gpuRes := &cliv1.Resources{Gpu: &cliv1.GpuResource{Devices: "all"}}
	for _, c := range []struct {
		kind         cliv1.PlacementKind
		res          *cliv1.Resources
		source       cliv1.OfferSource
		wantOffers   int
		notProbeable bool
	}{
		{cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, gpuRes, cliv1.OfferSource_OFFER_SOURCE_PROBED, 0, false},
		{cliv1.PlacementKind_PLACEMENT_KIND_SSH, nil, cliv1.OfferSource_OFFER_SOURCE_PROBED, 0, false},
		{cliv1.PlacementKind_PLACEMENT_KIND_SLURM, gpuRes, cliv1.OfferSource_OFFER_SOURCE_DECLARED, 1, false},
		{cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER, nil, cliv1.OfferSource_OFFER_SOURCE_DECLARED, 0, true},
	} {
		got := placementProbe(&cliv1.Placement{Kind: c.kind, Resources: c.res, Slurm: &cliv1.SlurmSettings{Gres: "gpu:h100:4"}}, nil)
		if got.Source != c.source {
			t.Errorf("%v: source %v, want %v", c.kind, got.Source, c.source)
		}
		declared, ok := got.Probe.(app.DeclaredOffers)
		if ok != (c.source == cliv1.OfferSource_OFFER_SOURCE_DECLARED) {
			t.Errorf("%v: probe is %T", c.kind, got.Probe)
			continue
		}
		if ok && (len(declared.Declared) != c.wantOffers || errors.Is(declared.Err, app.ErrNotProbeable) != c.notProbeable) {
			t.Errorf("%v: %v, %v", c.kind, declared.Declared, declared.Err)
		}
	}
}
