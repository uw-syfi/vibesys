package production

import (
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// test-isolation: declaredOffers is wiring that RunDaemon reaches only with a real runtime directory.

// Every alias of the registry, in any gres spelling, is the model of its row; a token the registry
// does not name is the unknown model and keeps its text.
func TestGresTokensResolveThroughTheRegistry(t *testing.T) {
	rng := rand.New(rand.NewPCG(31, 32))
	resources := map[core.AcceleratorVendor]*cliv1.Resources{
		core.VendorNVIDIA: {Gpu: &cliv1.GpuResource{Devices: "all"}},
		core.VendorAMD:    {Rocm: &cliv1.RocmResource{}},
	}
	for _, row := range core.Accelerators() {
		res, probed := resources[row.Vendor]
		for _, alias := range row.Aliases {
			if !probed {
				t.Fatalf("%s has aliases and no gres resource", row.Display)
			}
			for range 100 {
				token := alias
				switch rng.IntN(3) {
				case 0:
					token = strings.ToUpper(alias)
				case 1:
					token = alias + []string{"_80gb", "-sxm", "_192g"}[rng.IntN(3)]
				}
				gres := fmt.Sprintf("gpu:%s:%d(S:0-1)", token, 1+rng.IntN(8))
				got, err := declaredOffers(&cliv1.SlurmSettings{Gres: gres}, res)
				if err != nil || len(got) != 1 || got[0].Model != row.Model || got[0].Vendor != row.Vendor {
					t.Fatalf("%s as %q: %+v, %v", row.Display, gres, got, err)
				}
			}
			unknown := alias + "9"
			got, err := declaredOffers(&cliv1.SlurmSettings{Gres: "gpu:" + unknown + ":2"}, res)
			if err != nil || len(got) != 1 || got[0].Model != core.ModelOther || got[0].DeviceName != unknown {
				t.Errorf("%q: %+v, %v", unknown, got, err)
			}
		}
	}
}
