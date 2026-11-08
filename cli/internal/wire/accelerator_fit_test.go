package wire_test

import (
	"math/rand/v2"
	"strings"
	"testing"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// The orchestrator's fit rule compares a need's vendor and model with an offer's, as the contract
// carries them. A task authored with a display name and a machine probed with any alias of the same
// model therefore give the same vendor and model, so fit is the same whichever of them is the source.
// (The rule itself is in the orchestrator module, which cannot import this module's internals.)
func TestADisplayNameAndAnyProbeAliasGiveTheSameVendorAndModel(t *testing.T) {
	rng := rand.New(rand.NewPCG(41, 42))
	checked := 0
	for _, row := range core.Accelerators() {
		need, ok := task.LookupAccelerator(row.Display)
		if !ok {
			t.Fatalf("%s is not found by display name", row.Display)
		}
		for _, alias := range row.Aliases {
			for range 50 {
				device := strings.ToUpper(alias) + " 80GB"
				if rng.IntN(2) == 0 {
					device = alias
				}
				probed, err := core.ParseAccelerator(row.Vendor, device, "2")
				if err != nil {
					t.Fatal(err)
				}
				offer := wire.AcceleratorOffer(probed)
				if err := protovalidate.Validate(offer); err != nil {
					t.Fatalf("%q: %v", device, err)
				}
				if offer.GetVendor() != need.Vendor || offer.GetModel() != need.Model || offer.GetModel() == cliv1.AcceleratorModel_ACCELERATOR_MODEL_OTHER {
					t.Fatalf("%s authored is %v/%v, probed as %q is %v/%v", row.Display, need.Vendor, need.Model, device, offer.GetVendor(), offer.GetModel())
				}
				checked++
			}
		}
	}
	if checked == 0 {
		t.Fatal("nothing was checked")
	}
}
