package core_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Over random placements, needs and restrictions: the eligible placements are exactly the offers
// that give every need, sorted by name, or the one a restriction names when the config has it and
// it gives every need. Several eligible placements are no refusal; none is, and so is a
// restriction the config lacks or that lacks a need, each as invalid input that says why.
func TestTheEligiblePlacementsAreThoseThatGiveWhatTheTaskNeeds(t *testing.T) {
	r := rand.New(rand.NewPCG(9, 10))
	kinds := core.ResourceKinds()
	subset := func() []core.ResourceKind {
		var out []core.ResourceKind
		for _, k := range kinds {
			if r.IntN(2) == 0 {
				out = append(out, k)
			}
		}
		return out
	}
	for range 5000 {
		var offers []core.PlacementOffer
		for _, i := range r.Perm(r.IntN(4)) {
			offers = append(offers, core.PlacementOffer{Name: fmt.Sprintf("p%d", i), Resources: subset()})
		}
		needs := subset()
		restriction := []string{"", "", "p0", "p1", "p3", "nope"}[r.IntN(6)]
		got, err := core.EligiblePlacements(offers, needs, restriction)
		var fit []string
		for _, o := range offers {
			if o.Lacks(needs) == "" {
				fit = append(fit, o.Name)
			}
		}
		slices.Sort(fit)
		at := slices.IndexFunc(offers, func(o core.PlacementOffer) bool { return o.Name == restriction })
		want := fit
		if restriction != "" {
			want = nil
			if at >= 0 && offers[at].Lacks(needs) == "" {
				want = []string{restriction}
			}
		}
		if (err == nil) != (len(want) > 0) || !slices.Equal(got, want) {
			t.Fatalf("offers %+v, needs %v, restriction %q: %q, %v; want %q", offers, needs, restriction, got, err, want)
		}
		if err != nil {
			f := core.AsFailure(err)
			if f.Kind != core.FailureInvalidInput {
				t.Fatalf("a refusal of kind %s: %v", f.Kind, err)
			}
			if restriction != "" && at >= 0 {
				if want := fmt.Sprintf("placement %q has no resource %q", restriction, offers[at].Lacks(needs)); !strings.Contains(f.Message, want) {
					t.Fatalf("%v, want it to say %s", err, want)
				}
			}
		}
	}
}

func TestAPlacementNameIsAKeyAndPartOfAnID(t *testing.T) {
	for _, ok := range []string{"default", "a100", "local-kind", "x_1"} {
		if err := core.ValidPlacementName(ok); err != nil {
			t.Errorf("%q: %v", ok, err)
		}
	}
	for _, bad := range []string{"", "A100", "-x", "a b", "a/b", "a.b", strings.Repeat("x", 64)} {
		if err := core.ValidPlacementName(bad); err == nil {
			t.Errorf("%q was taken for a placement name", bad)
		}
	}
}
