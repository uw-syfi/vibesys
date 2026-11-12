package task_test

import (
	"testing"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// The registry is closed over the contract: every model the contract defines, except the unnamed and
// OTHER, has exactly one display name, so adding a model without a name (or a name without a
// model) fails here.
func TestEveryModelOfTheContractHasOneName(t *testing.T) {
	named := map[cliv1.AcceleratorModel][]string{}
	for _, n := range task.AcceleratorNames() {
		a, _ := task.LookupAccelerator(n)
		if a.Model != cliv1.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED {
			named[a.Model] = append(named[a.Model], n)
		}
	}
	for m, name := range cliv1.AcceleratorModel_name {
		model := cliv1.AcceleratorModel(m)
		if model == cliv1.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED || model == cliv1.AcceleratorModel_ACCELERATOR_MODEL_OTHER {
			if len(named[model]) != 0 {
				t.Errorf("%s must have no display name, has %v", name, named[model])
			}
			continue
		}
		if len(named[model]) != 1 {
			t.Errorf("%s has names %v, want exactly one", name, named[model])
		}
	}
}

// What a name names is a legal need under the contract's own vendor-and-model rule, a CPU names no
// model, and a name of a whole vendor ends in GPU.
func TestEveryRegisteredIdentityIsALegalNeed(t *testing.T) {
	for _, n := range task.AcceleratorNames() {
		a, ok := task.LookupAccelerator(n)
		if !ok {
			t.Fatalf("%s listed and not found", n)
		}
		need := &cliv1.AcceleratorNeed{Vendor: a.Vendor, Model: a.Model, Count: 1}
		if err := protovalidate.Validate(need); err != nil {
			t.Errorf("%s: %v", n, err)
		}
		if a.Vendor == cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_CPU && a.Model != cliv1.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED {
			t.Errorf("%s: a CPU names a model", n)
		}
	}
	if _, ok := task.LookupAccelerator("NVIDIA GPU"); !ok {
		t.Error("the vendor-only name is gone")
	}
}

// The registry holds every name the examples author, so a new example with a known device needs no
// change here, and a name not in it is refused.
func TestTheRegistryIsSeededFromTheExamples(t *testing.T) {
	for _, n := range []string{"AMD MI210", "AMD MI250", "AMD MI300A", "AMD MI300X", "AWS Trainium2", "Apple Silicon", "CPU", "NVIDIA B200", "NVIDIA GPU", "NVIDIA H100", "NVIDIA L4"} {
		if _, ok := task.LookupAccelerator(n); !ok {
			t.Errorf("%q is not registered", n)
		}
	}
	if _, ok := task.LookupAccelerator("NVIDIA H200"); ok {
		t.Error("an unknown name was found")
	}
}
