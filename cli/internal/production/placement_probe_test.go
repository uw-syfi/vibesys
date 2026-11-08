package production

import (
	"errors"
	"strings"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// test-isolation: placementProbe is wiring that RunDaemon reaches only with a real runtime directory.

// A Slurm or Apptainer placement reports what its config declares and never probes the login node;
// every other kind is probed through its own resources.
func TestOnlyClusterPlacementsReportDeclaredOffers(t *testing.T) {
	gpuRes := &cliv1.Resources{Gpu: &cliv1.GpuResource{Devices: "all"}}
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
		got, err := placementProbe(&cliv1.Placement{Kind: c.kind, Resources: c.res, Slurm: &cliv1.SlurmSettings{Gres: "gpu:h100:4"}}, nil)
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
	if _, err := placementProbe(&cliv1.Placement{}, nil); err == nil {
		t.Error("an unspecified kind was accepted")
	}
}

func TestDeclaredOffersReadTheGresOfTheDeclaredResource(t *testing.T) {
	gpuRes := &cliv1.Resources{Gpu: &cliv1.GpuResource{Devices: "all"}}
	rocmRes := &cliv1.Resources{Rocm: &cliv1.RocmResource{}}
	slurm := func(gres string) *cliv1.SlurmSettings { return &cliv1.SlurmSettings{Gres: gres} }
	type want struct {
		model core.AcceleratorModel
		count uint32
	}
	for _, c := range []struct {
		name  string
		slurm *cliv1.SlurmSettings
		res   *cliv1.Resources
		want  []want
		err   string
	}{
		{"nvidia model", slurm("gpu:h100:8"), gpuRes, []want{{core.ModelH100, 8}}, ""},
		{"amd with a socket suffix", slurm("gpu:mi300x:4(S:0-1)"), rocmRes, []want{{core.ModelMI300X, 4}}, ""},
		{"no model", slurm("gpu:2"), gpuRes, []want{{core.ModelOther, 2}}, ""},
		{"two models and a non-gpu item", slurm("gpu:a100:2,shard:4,gpu:h100:1"), gpuRes, []want{{core.ModelA100, 2}, {core.ModelH100, 1}}, ""},
		{"no declaration", slurm("gpu:h100:8"), &cliv1.Resources{}, nil, "no declared accelerators"},
		{"no resources at all", nil, nil, nil, "no declared accelerators"},
		{"no gres", nil, gpuRes, nil, "no gpu count"},
		{"gres without gpu", slurm("shard:4"), gpuRes, nil, "no gpu count"},
		{"zero count", slurm("gpu:h100:0"), gpuRes, nil, "gres"},
		{"both vendors", slurm("gpu:2"), &cliv1.Resources{Gpu: &cliv1.GpuResource{Devices: "all"}, Rocm: &cliv1.RocmResource{}}, nil, "cannot be told apart"},
	} {
		got, err := declaredOffers(c.slurm, c.res)
		if c.err != "" {
			if err == nil || !strings.Contains(err.Error(), c.err) || (c.err == "no declared accelerators") != errors.Is(err, errNoDeclaredAccelerators) {
				t.Errorf("%s: %v, want error %q", c.name, err, c.err)
			}
			continue
		}
		if err != nil || len(got) != len(c.want) {
			t.Errorf("%s: %v, %v", c.name, got, err)
			continue
		}
		for i, o := range got {
			if o.Model != c.want[i].model || o.Count != c.want[i].count {
				t.Errorf("%s: offer %d is %v", c.name, i, o)
			}
		}
	}
}
