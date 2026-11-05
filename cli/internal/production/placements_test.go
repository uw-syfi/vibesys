package production

import (
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// test-isolation: machineEnvironments is the wiring from the operator config to each placement's
// environment, which RunDaemon reaches only with a real runtime directory and index.

// Every placement of every kind is wired without reaching Docker or another machine, and offers the
// resources its section names, the AMD GPUs of an apptainer placement among them, with the claims
// its resources make.
func TestEveryPlacementKindIsWired(t *testing.T) {
	p := Process{UID: 1000, GID: 1000, Home: "/home/u", Executable: "/usr/bin/vibesys-cli", Getenv: func(string) string { return "" }}
	for _, text := range configs {
		op := parse(t, text)
		m, err := p.machineEnvironments(op, clock.System{}, runtimedir.Dir{}, "key")
		if err != nil {
			t.Fatalf("config %q: %v", text, err)
		}
		if len(m.offers) != len(op.GetPlacements()) {
			t.Fatalf("config %q: offers %+v", text, m.offers)
		}
		for _, offer := range m.offers {
			pl := op.GetPlacements()[offer.Name]
			if rocm := pl.GetResources().GetRocm() != nil; rocm != slices.Contains(offer.Resources, core.ResourceROCm) {
				t.Errorf("config %q: placement %s offers %v", text, offer.Name, offer.Resources)
			}
		}
		if got, want := m.claims, claimsOf(t, text); len(want) > 0 && !sameClaims(got, want) {
			t.Errorf("config %q: claims %v, want %v", text, got, want)
		}
	}
}

func sameClaims(a, b map[string][]core.Claim) bool {
	for name, cs := range b {
		if !slices.Equal(a[name], cs) {
			return false
		}
	}
	return true
}
