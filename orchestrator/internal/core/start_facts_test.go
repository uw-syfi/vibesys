package core_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

func genNeed(r *rand.Rand) *core.AcceleratorNeed {
	if r.IntN(3) == 0 {
		return nil
	}
	vendors := []core.AcceleratorVendor{core.VendorNvidia, core.VendorAMD, core.VendorAWS, core.VendorApple, core.VendorCPU}
	models := []core.AcceleratorModel{"", core.ModelMI300X, core.ModelH100, core.ModelOther}
	return &core.AcceleratorNeed{Vendor: vendors[r.IntN(len(vendors))], Model: models[r.IntN(len(models))], Count: uint32(1 + r.IntN(8))}
}

// genCard is a card, or none: the explicit state of a task the host could not read a card of.
func genCard(r *rand.Rand) *core.TaskCard {
	if r.IntN(5) == 0 {
		return nil
	}
	return &core.TaskCard{
		Task: fmt.Sprintf("task-%d", r.IntN(100)), Domain: core.DomainLLMServing, Hardware: genNeed(r),
		HardwareName: "AMD MI300X", Heading: "A heading",
		Objectives: []core.CardObjective{{Name: "throughput", Direction: core.Maximize, Unit: "tok/s"}},
	}
}

func genPlaces(r *rand.Rand) []core.PlaceVerdict {
	var out []core.PlaceVerdict
	for _, i := range r.Perm(6)[:r.IntN(6)] {
		out = append(out, core.PlaceVerdict{Name: fmt.Sprintf("place-%d", i), Fits: r.IntN(2) == 0})
	}
	return out
}

// chosenOf names one of the places, one the host has not got, or none.
func chosenOf(r *rand.Rand, places []core.PlaceVerdict) string {
	switch k := r.IntN(4); {
	case k == 0:
		return ""
	case k == 1 || len(places) == 0:
		return "elsewhere"
	default:
		return places[r.IntN(len(places))].Name
	}
}

// Whatever the card, the places and the choice, a start is refused exactly when the rule says, a
// place that does not fit is never recorded for a task that needs an accelerator, a refusal names what
// it refuses and nothing is recorded with it, and an accepted start records the card it was given.
func TestAStartIsRefusedForAPlaceThatDoesNotFitAndNeverRecordsOne(t *testing.T) {
	r := rand.New(rand.NewPCG(5, 8))
	for i := range 3000 {
		card, places := genCard(r), genPlaces(r)
		chosen := chosenOf(r, places)
		got := core.ResolveStartFacts(card, chosen, places)

		verdict := map[string]bool{}
		var fitting []string
		for _, p := range places {
			verdict[p.Name] = p.Fits
			if p.Fits {
				fitting = append(fitting, p.Name)
			}
		}
		sort.Strings(fitting)
		var need *core.AcceleratorNeed
		if card != nil {
			need = card.Hardware
		}
		_, has := verdict[chosen]
		var want core.Refusal
		switch {
		case chosen != "" && !has:
			want = core.RefuseEvalPlaceNotFound
		case chosen != "" && need != nil && !verdict[chosen]:
			want = core.RefuseEvalPlaceMisfit
		case chosen == "" && need != nil && len(fitting) == 0:
			want = core.RefuseEvalPlaceMisfit
		case chosen == "" && need != nil && len(fitting) > 1:
			want = core.RefuseEvalPlaceAmbiguous
		}
		if want != "" {
			if got.Refused == nil || got.Refused.Refusal != want || got.Card != nil || got.EvalPlace != "" {
				t.Fatalf("#%d card %+v chosen %q places %v: got %+v, want a refusal %s with nothing recorded", i, card, chosen, places, got, want)
			}
			text := got.Refused.Error()
			if chosen != "" && !strings.Contains(text, chosen) || need != nil && !strings.Contains(text, need.String()) {
				t.Fatalf("#%d the refusal %q names neither the place %q nor the need %v", i, text, chosen, need)
			}
			if !sort.StringsAreSorted(got.Refused.Places) {
				t.Fatalf("#%d refused places %v are not in name order", i, got.Refused.Places)
			}
			continue
		}
		if got.Refused != nil || got.Card != card {
			t.Fatalf("#%d card %+v chosen %q places %v: got %+v, want the card recorded and no refusal", i, card, chosen, places, got)
		}
		if need != nil && !verdict[got.EvalPlace] {
			t.Fatalf("#%d recorded place %q does not fit %v", i, got.EvalPlace, need)
		}
		if chosen != "" && got.EvalPlace != chosen || chosen == "" && need == nil && got.EvalPlace != "" {
			t.Fatalf("#%d chosen %q need %v: recorded %q", i, chosen, need, got.EvalPlace)
		}
	}
}

// A task with no card is an explicit state, recorded as such: the run has no card, whatever place
// was named, and a named place the host has is kept without a fit judgment.
func TestAStartWithNoCardRecordsNoCardAndKeepsANamedPlace(t *testing.T) {
	w := newScript(t)
	r := w.command(operator, core.StartRun{Repository: "repo", Task: "t", FanoutLimit: 1, Lead: lead, EvalPlace: "a",
		Facts: core.ResolveStartFacts(nil, "a", []core.PlaceVerdict{{Name: "a"}})})
	run, _ := w.s.Run(r.Run)
	if r.Refusal != "" || run.TaskCard != nil || run.EvalPlace != "a" {
		t.Fatalf("receipt %+v run %+v, want accepted with no card and place a", r, run)
	}
}

// Through Decide: a refused place makes no run and the receipt lists the places; an accepted start
// records the facts on the run, and nothing that happens to the run afterwards changes them.
func TestStartFactsAreRecordedOnceAndNeverChangeAfterStart(t *testing.T) {
	rng := rand.New(rand.NewPCG(11, 2))
	for i := range 300 {
		card, places := genCard(rng), genPlaces(rng)
		chosen := chosenOf(rng, places)
		facts := core.ResolveStartFacts(card, chosen, places)
		w := newScript(t)
		rc := w.command(operator, core.StartRun{Repository: "repo", Task: "t", FanoutLimit: 8, Lead: lead, Brief: "go", EvalPlace: chosen, Facts: facts})
		if facts.Refused != nil {
			if rc.Refusal != facts.Refused.Refusal || !slices.Equal(rc.EvalPlaces, facts.Refused.Places) || len(w.s.Runs()) != 0 {
				t.Fatalf("#%d receipt %+v for refusal %+v, runs %d", i, rc, facts.Refused, len(w.s.Runs()))
			}
			continue
		}
		w.open(rc.Agent, steers)
		want, _ := w.s.Run(rc.Run)
		if !reflect.DeepEqual(want.TaskCard, card) || want.EvalPlace != facts.EvalPlace {
			t.Fatalf("#%d run records card %+v place %q, want %+v %q", i, want.TaskCard, want.EvalPlace, card, facts.EvalPlace)
		}
		w.endTurn(rc.Agent, core.TurnCompleted)
		for range 6 {
			switch rng.IntN(3) {
			case 0:
				w.spawn(rc.Run, rc.Agent, steers)
			case 1:
				w.command(operator, core.SendMessage{Agent: rc.Agent, Text: "hello"})
			default:
				w.command(operator, core.StartRun{Repository: "repo", Task: "t", FanoutLimit: 1, Lead: lead})
			}
			if got, _ := w.s.Run(rc.Run); !reflect.DeepEqual(got.TaskCard, want.TaskCard) || got.EvalPlace != want.EvalPlace {
				t.Fatalf("#%d the start facts changed: card %+v place %q, want %+v %q", i, got.TaskCard, got.EvalPlace, want.TaskCard, want.EvalPlace)
			}
		}
		w.command(operator, core.StopRun{Run: rc.Run})
		if got, _ := w.s.Run(rc.Run); !reflect.DeepEqual(got.TaskCard, want.TaskCard) || got.EvalPlace != want.EvalPlace {
			t.Fatalf("#%d the start facts changed by the stop", i)
		}
	}
}

// A start that names a place the shell did not resolve, or resolved to another one, is refused rather
// than recorded as the client's guess.
func TestAPlaceNamedButNotResolvedIsRefusedNotRecorded(t *testing.T) {
	w := newScript(t)
	r := w.command(operator, core.StartRun{Repository: "repo", Task: "t", FanoutLimit: 1, Lead: lead, EvalPlace: "a"})
	if r.Refusal != core.RefuseEvalPlaceNotFound || len(w.s.Runs()) != 0 {
		t.Fatalf("receipt %+v, runs %d: want a refusal as not found and no run", r, len(w.s.Runs()))
	}
}
