package resource_test

import (
	"errors"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"strings"
	"testing"

	"buf.build/go/protovalidate"
)

func TestAcceleratorOffersUseOnlyWholeModelTokens(t *testing.T) {
	for _, c := range []struct {
		vendor      cliv1.AcceleratorVendor
		name, count string
		model       cliv1.AcceleratorModel
		invalid     bool
	}{
		{cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, "NVIDIA H100 80GB HBM3", "8", cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100, false},
		{cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, "NVIDIA L40", "1", cliv1.AcceleratorModel_ACCELERATOR_MODEL_OTHER, false},
		{cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, "AMD Instinct MI300X", "2", cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X, false},
		{cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, "unread", "unread", 0, true},
		{cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, "MI210", "0", 0, true},
	} {
		got, err := resource.AcceleratorOffer(c.vendor, c.name, c.count)
		if c.invalid {
			if err == nil {
				t.Errorf("%q: expected error", c.name)
			}
			continue
		}
		if err != nil || got.GetModel() != c.model || got.GetVendor() != c.vendor || got.GetDeviceName() != c.name {
			t.Errorf("%q: %v, %v", c.name, got, err)
		}
	}
}

func TestDeclaredOffersReadTheGresOfTheDeclaredResource(t *testing.T) {
	gpuRes := &cliv1.Resources{Gpu: &cliv1.GpuResource{Devices: "all"}}
	rocmRes := &cliv1.Resources{Rocm: &cliv1.RocmResource{}}
	slurm := func(gres string) *cliv1.SlurmSettings { return &cliv1.SlurmSettings{Gres: gres} }
	type want struct {
		model cliv1.AcceleratorModel
		count uint32
	}
	for _, c := range []struct {
		name  string
		slurm *cliv1.SlurmSettings
		res   *cliv1.Resources
		want  []want
		err   string
	}{
		{"nvidia model", slurm("gpu:h100:8"), gpuRes, []want{{cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100, 8}}, ""},
		{"amd with a socket suffix", slurm("gpu:mi300x:4(S:0-1)"), rocmRes, []want{{cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X, 4}}, ""},
		{"no model", slurm("gpu:2"), gpuRes, []want{{cliv1.AcceleratorModel_ACCELERATOR_MODEL_OTHER, 2}}, ""},
		{"two models and a non-gpu item", slurm("gpu:a100:2,shard:4,gpu:h100:1"), gpuRes, []want{{cliv1.AcceleratorModel_ACCELERATOR_MODEL_A100, 2}, {cliv1.AcceleratorModel_ACCELERATOR_MODEL_H100, 1}}, ""},
		{"no declaration", slurm("gpu:h100:8"), &cliv1.Resources{}, nil, "no declared accelerators"},
		{"no resources at all", nil, nil, nil, "no declared accelerators"},
		{"no gres", nil, gpuRes, nil, "no gpu count"},
		{"gres without gpu", slurm("shard:4"), gpuRes, nil, "no gpu count"},
		{"zero count", slurm("gpu:h100:0"), gpuRes, nil, "gres"},
		{"both vendors", slurm("gpu:2"), &cliv1.Resources{Gpu: &cliv1.GpuResource{Devices: "all"}, Rocm: &cliv1.RocmResource{}}, nil, "cannot be told apart"},
	} {
		got, err := resource.DeclaredOffers(c.slurm, c.res)
		if c.err != "" {
			if err == nil || !strings.Contains(err.Error(), c.err) || (c.err == "no declared accelerators") != errors.Is(err, resource.ErrNoDeclaredAccelerators) {
				t.Errorf("%s: %v, want error %q", c.name, err, c.err)
			}
			continue
		}
		if err != nil || len(got) != len(c.want) {
			t.Errorf("%s: %v, %v", c.name, got, err)
			continue
		}
		for i, o := range got {
			if o.GetModel() != c.want[i].model || o.GetCount() != c.want[i].count {
				t.Errorf("%s: offer %d is %v", c.name, i, o)
			}
			if err := protovalidate.Validate(o); err != nil {
				t.Errorf("%s: offer %d is invalid: %v", c.name, i, err)
			}
		}
	}
}
