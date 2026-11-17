package production

import (
	"errors"
	"strings"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
)

// test-isolation: placementProbe is wiring that RunDaemon reaches only with a real runtime directory.

// poolOf is the pool of GPUs of a vendor a file declares, with a count and a GRES type.
func poolOf(t *testing.T, vendor core.AcceleratorVendor, count int, gresType string) operator.Pool {
	t.Helper()
	size, err := core.ParseGPUCount(uint32(count), "count")
	if err != nil {
		t.Fatal(err)
	}
	return operator.Pool{Vendor: vendor, Size: size, GresType: gresType, Key: "placements.x.resources.gpu.count"}
}

// A Slurm or Apptainer placement reports what its config declares and never probes the login node;
// every other kind is probed through its own resources.
func TestOnlyClusterPlacementsReportDeclaredOffers(t *testing.T) {
	four := uint32(4)
	gpuRes := &cliv1.Resources{Gpu: &cliv1.GpuPool{Count: &four, GresType: "h100"}}
	for _, c := range []struct {
		kind         cliv1.PlacementKind
		res          *cliv1.Resources
		source       core.OfferSource
		wantOffers   int
		notProbeable bool
	}{
		{cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, gpuRes, core.OfferProbed, 0, false},
		{cliv1.PlacementKind_PLACEMENT_KIND_SSH, nil, core.OfferProbed, 0, false},
		{cliv1.PlacementKind_PLACEMENT_KIND_SLURM, gpuRes, core.OfferDeclared, 1, false},
		{cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER, nil, core.OfferDeclared, 0, true},
	} {
		got, err := placementProbe("x", &cliv1.Placement{Kind: c.kind, Resources: c.res, Slurm: &cliv1.SlurmSettings{}}, nil)
		if err != nil {
			t.Fatal(err)
		}
		if got.Source != c.source {
			t.Errorf("%v: source %v, want %v", c.kind, got.Source, c.source)
		}
		declared, ok := got.Probe.(app.DeclaredOffers)
		if ok != (c.source == core.OfferDeclared) {
			t.Errorf("%v: probe is %T", c.kind, got.Probe)
			continue
		}
		if ok && (len(declared.Declared) != c.wantOffers || errors.Is(declared.Err, app.ErrNotProbeable) != c.notProbeable) {
			t.Errorf("%v: %v, %v", c.kind, declared.Declared, declared.Err)
		}
	}
	if _, err := placementProbe("x", &cliv1.Placement{}, nil); err == nil {
		t.Error("an unspecified kind was accepted")
	}
}

// A cluster placement declares the GPUs of its pool: the vendor of its table, its count, and the
// model its GRES type names.
func TestDeclaredOffersReadTheGPUPool(t *testing.T) {
	type want struct {
		model core.AcceleratorModel
		count uint32
	}
	for _, c := range []struct {
		name string
		pool operator.Pool
		want []want
		err  string
	}{
		{"nvidia model", poolOf(t, core.VendorNVIDIA, 8, "h100"), []want{{core.ModelH100, 8}}, ""},
		{"amd model", poolOf(t, core.VendorAMD, 4, "mi300x"), []want{{core.ModelMI300X, 4}}, ""},
		{"no model", poolOf(t, core.VendorNVIDIA, 2, ""), []want{{core.ModelOther, 2}}, ""},
		{"no pool", operator.Pool{}, nil, "no declared accelerators"},
	} {
		got, err := declaredOffers(c.pool)
		if c.err != "" {
			if err == nil || !strings.Contains(err.Error(), c.err) || !errors.Is(err, errNoDeclaredAccelerators) {
				t.Errorf("%s: %v, want error %q", c.name, err, c.err)
			}
			continue
		}
		if err != nil || len(got) != len(c.want) {
			t.Errorf("%s: %v, %v", c.name, got, err)
			continue
		}
		for i, o := range got {
			if o.Model != c.want[i].model || o.Count != c.want[i].count || o.Vendor != c.pool.Vendor {
				t.Errorf("%s: offer %d is %v", c.name, i, o)
			}
		}
	}
}
