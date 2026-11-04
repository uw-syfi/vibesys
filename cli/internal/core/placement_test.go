package core_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Over random placements, needs and requests: a requested placement is chosen exactly when the
// config has it and it gives every need; with none requested, the one placement that gives every
// need is chosen, and none or several is a refusal that names them. A choice is never empty, and a
// refusal is always invalid input.
func TestThePlacementChosenGivesWhatTheTaskNeeds(t *testing.T) {
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
		for i := range r.IntN(4) {
			offers = append(offers, core.PlacementOffer{Name: fmt.Sprintf("p%d", i), Resources: subset()})
		}
		needs := subset()
		requested := []string{"", "", "p0", "p1", "p3", "nope"}[r.IntN(6)]
		got, err := core.ChoosePlacement(offers, needs, requested)
		var fit []string
		for _, o := range offers {
			if o.Lacks(needs) == "" {
				fit = append(fit, o.Name)
			}
		}
		at := slices.IndexFunc(offers, func(o core.PlacementOffer) bool { return o.Name == requested })
		var wantOK bool
		switch {
		case requested != "":
			wantOK = at >= 0 && offers[at].Lacks(needs) == ""
		default:
			wantOK = len(fit) == 1
		}
		if (err == nil) != wantOK {
			t.Fatalf("offers %+v, needs %v, requested %q: %q, %v; want ok=%v", offers, needs, requested, got, err, wantOK)
		}
		if err != nil {
			f := core.AsFailure(err)
			if f.Kind != core.FailureInvalidInput {
				t.Fatalf("a refusal of kind %s: %v", f.Kind, err)
			}
			if requested != "" && at >= 0 {
				if want := fmt.Sprintf("placement %q has no resource %q", requested, offers[at].Lacks(needs)); !strings.Contains(f.Message, want) {
					t.Fatalf("%v, want it to say %s", err, want)
				}
			}
			if requested == "" && len(fit) > 1 && !strings.Contains(f.Message, strings.Join(fit, ", ")) {
				t.Fatalf("%v, want it to name %v", err, fit)
			}
			continue
		}
		if got == "" || (requested != "" && got != requested) || (requested == "" && got != fit[0]) {
			t.Fatalf("offers %+v, needs %v, requested %q: chose %q", offers, needs, requested, got)
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
