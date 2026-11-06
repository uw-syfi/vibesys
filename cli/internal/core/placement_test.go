package core_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Over random placements, job kinds, needs and restrictions: the eligible placements are exactly
// the offers that run the job's kind (every kind when they name none) and give every need, sorted
// by name, or the one a restriction names when the config has it, it runs the kind and it gives
// every need. Several eligible placements are no refusal; none is, and so is a restriction the
// config lacks, that runs no job of the kind or that lacks a need, each as invalid input that says
// why, naming the placement and the kind.
func TestTheEligiblePlacementsAreThoseThatRunTheKindAndGiveWhatTheTaskNeeds(t *testing.T) {
	r := rand.New(rand.NewPCG(9, 10))
	kinds := core.ResourceKinds()
	jobKinds := []core.JobKind{core.JobCheck, core.JobCommand, core.JobMeasure}
	subset := func() []core.ResourceKind {
		var out []core.ResourceKind
		for _, k := range kinds {
			if r.IntN(2) == 0 {
				out = append(out, k)
			}
		}
		return out
	}
	jobSubset := func() []core.JobKind {
		var out []core.JobKind
		for _, k := range jobKinds {
			if r.IntN(3) == 0 {
				out = append(out, k)
			}
		}
		return out
	}
	for range 5000 {
		var offers []core.PlacementOffer
		for _, i := range r.Perm(r.IntN(4)) {
			offers = append(offers, core.PlacementOffer{Name: fmt.Sprintf("p%d", i), Resources: subset(), Jobs: jobSubset()})
		}
		needs := subset()
		kind := jobKinds[r.IntN(len(jobKinds))]
		restriction := []string{"", "", "p0", "p1", "p3", "nope"}[r.IntN(6)]
		got, err := core.EligiblePlacements(offers, kind, needs, restriction)
		runs := func(o core.PlacementOffer) bool { return len(o.Jobs) == 0 || slices.Contains(o.Jobs, kind) }
		var fit []string
		for _, o := range offers {
			if runs(o) && o.Lacks(needs) == "" {
				fit = append(fit, o.Name)
			}
		}
		slices.Sort(fit)
		at := slices.IndexFunc(offers, func(o core.PlacementOffer) bool { return o.Name == restriction })
		want := fit
		if restriction != "" {
			want = nil
			if at >= 0 && runs(offers[at]) && offers[at].Lacks(needs) == "" {
				want = []string{restriction}
			}
		}
		if (err == nil) != (len(want) > 0) || !slices.Equal(got, want) {
			t.Fatalf("offers %+v, kind %s, needs %v, restriction %q: %q, %v; want %q", offers, kind, needs, restriction, got, err, want)
		}
		if err != nil {
			f := core.AsFailure(err)
			if f.Kind != core.FailureInvalidInput {
				t.Fatalf("a refusal of kind %s: %v", f.Kind, err)
			}
			var say string
			switch {
			case restriction != "" && at >= 0 && !runs(offers[at]):
				say = fmt.Sprintf("placement %q runs no %s job", restriction, kind)
			case restriction != "" && at >= 0:
				say = fmt.Sprintf("placement %q has no resource %q", restriction, offers[at].Lacks(needs))
			case restriction == "" && !slices.ContainsFunc(offers, runs):
				say = fmt.Sprintf("runs a %s job", kind)
			}
			if !strings.Contains(f.Message, say) {
				t.Fatalf("%v, want it to say %s", err, say)
			}
		}
	}
}

// A placement that names its job kinds runs those and no other; one that names none runs every
// kind, as every placement did before job_kinds.
func TestAPlacementRunsTheJobKindsItNamesOrEveryKind(t *testing.T) {
	short := core.PlacementOffer{Name: "devel", Jobs: []core.JobKind{core.JobCheck, core.JobCommand}}
	all := core.PlacementOffer{Name: "batch"}
	for _, k := range []core.JobKind{core.JobCheck, core.JobCommand, core.JobMeasure} {
		if !all.Runs(k) {
			t.Errorf("a placement that names no kind does not run %s", k)
		}
		if short.Runs(k) == (k == core.JobMeasure) {
			t.Errorf("devel runs %s: %v", k, short.Runs(k))
		}
	}
	got, err := core.EligiblePlacements([]core.PlacementOffer{short, all}, core.JobMeasure, nil, "")
	if err != nil || !slices.Equal(got, []string{"batch"}) {
		t.Fatalf("a measure may run on %q, %v; want batch alone", got, err)
	}
	_, err = core.EligiblePlacements([]core.PlacementOffer{short}, core.JobMeasure, nil, "")
	if f := core.AsFailure(err); err == nil || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "runs a measure job") {
		t.Fatalf("a measure with only devel: %v", err)
	}
	_, err = core.EligiblePlacements([]core.PlacementOffer{short, all}, core.JobMeasure, nil, "devel")
	if f := core.AsFailure(err); err == nil || !strings.Contains(f.Message, `placement "devel" runs no measure job (its job_kinds: check, command)`) {
		t.Fatalf("a measure restricted to devel: %v", err)
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
