package production

import (
	"math/rand/v2"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// test-isolation: declaredOffers is wiring that RunDaemon reaches only with a real runtime directory.

// Every alias of the registry, in any spelling a GRES type takes, is the model of its row; a type the
// registry does not name is the unknown model and keeps its text.
func TestGresTypesResolveThroughTheRegistry(t *testing.T) {
	rng := rand.New(rand.NewPCG(31, 32))
	declared := map[core.AcceleratorVendor]bool{core.VendorNVIDIA: true, core.VendorAMD: true}
	for _, row := range core.Accelerators() {
		for _, alias := range row.Aliases {
			if !declared[row.Vendor] {
				t.Fatalf("%s has aliases and no GPU table", row.Display)
			}
			for range 100 {
				token := alias
				switch rng.IntN(3) {
				case 0:
					token = strings.ToUpper(alias)
				case 1:
					token = alias + []string{"_80gb", "-sxm", "_192g"}[rng.IntN(3)]
				}
				count := 1 + rng.IntN(8)
				got, err := declaredOffers(poolOf(t, row.Vendor, count, token))
				if err != nil || len(got) != 1 || got[0].Model != row.Model || got[0].Vendor != row.Vendor || got[0].Count != uint32(count) {
					t.Fatalf("%s as %q: %+v, %v", row.Display, token, got, err)
				}
			}
			unknown := alias + "9"
			got, err := declaredOffers(poolOf(t, row.Vendor, 2, unknown))
			if err != nil || len(got) != 1 || got[0].Model != core.ModelOther || got[0].DeviceName != unknown {
				t.Errorf("%q: %+v, %v", unknown, got, err)
			}
		}
	}
}
