package core

import (
	"fmt"
	"slices"
)

// The start facts of a run: what the host establishes before a run starts and the run then records,
// once, and never changes. The shell reads the task's card and the places evaluations can run, asks
// its fit port which places give what the card needs, and hands the core the result as data; this file
// is the pure rule that turns it into the facts of the run or a refusal.

// PlaceVerdict is one place the host knows and whether the shell's fit port says it gives what the
// task needs. The verdict is data: the core holds no rule of fit.
type PlaceVerdict struct {
	Name string
	Fits bool
}

// StartFacts is what a start records on the run. The zero value is a start no shell resolved.
type StartFacts struct {
	// Card is the task's card as the host read it. Nil is the explicit state "the task has no card":
	// the host had no reader, or the task was not found or not supported by it. It is never a guess.
	Card *TaskCard
	// EvalPlace is the resolved place, empty when the run is unpinned.
	EvalPlace string
	// Refused is set when the start must be refused for its place. Card and EvalPlace are then unset.
	Refused *PlaceRefusal
}

// PlaceRefusal says why a start's eval place was refused. Its text names the place and the need.
type PlaceRefusal struct {
	Refusal Refusal
	// Place is the place named, empty when none was.
	Place string
	// Need is what the task wants, nil when the task states none.
	Need *AcceleratorNeed
	// Places are the places the refusal names, in name order: all the host has for a place not found,
	// the fitting ones for a misfit or an ambiguous choice.
	Places []string
}

func (e *PlaceRefusal) Error() string {
	place := "no place"
	if e.Place != "" {
		place = "eval place " + e.Place
	}
	switch e.Refusal {
	case RefuseEvalPlaceNotFound:
		return fmt.Sprintf("%s: the host has no such place (it has: %s); the task needs %s", place, listOrNone(e.Places), e.Need)
	case RefuseEvalPlaceAmbiguous:
		return fmt.Sprintf("%s was named and the task needs %s: several places give it, name one of %s", place, e.Need, listOrNone(e.Places))
	default:
		return fmt.Sprintf("%s does not give what the task needs, %s (places that do: %s)", place, e.Need, listOrNone(e.Places))
	}
}

func listOrNone(names []string) string {
	if len(names) == 0 {
		return "none"
	}
	out := names[0]
	for _, n := range names[1:] {
		out += ", " + n
	}
	return out
}

// String says the need as a task states it: a count, then the vendor and the model if it names one.
func (n *AcceleratorNeed) String() string {
	if n == nil {
		return "no accelerator"
	}
	out := fmt.Sprintf("%d %s", n.Count, n.Vendor)
	if n.Model != "" {
		out += " " + string(n.Model)
	}
	return out
}

// ResolveStartFacts decides the facts of a start. chosen is the place the client named, empty to let
// the host resolve it; places are every place the host knows, with the shell's verdict on each.
//
//   - A named place the host does not have is refused as not found.
//   - A task that needs an accelerator never starts without a resolved place: a named place that does
//     not fit is refused as a misfit; an unnamed one takes the only fitting place, and is refused as a
//     misfit when none fits and as ambiguous when several do.
//   - A task that needs none (a CPU need is none), or has no card, takes the place named, if the host has it, and is
//     unpinned otherwise. The fit of a place is not judged without a need, and that is the state the
//     run records, not a guess.
func ResolveStartFacts(card *TaskCard, chosen string, places []PlaceVerdict) StartFacts {
	var need *AcceleratorNeed
	if card != nil {
		need = card.Hardware
	}
	// A CPU need is no accelerator: a place named for it is still judged, but none is chosen for it.
	accelerator := need != nil && need.Vendor != VendorCPU
	var all, fitting []string
	var named *PlaceVerdict
	for i, p := range places {
		all = append(all, p.Name)
		if p.Fits {
			fitting = append(fitting, p.Name)
		}
		if p.Name == chosen {
			named = &places[i]
		}
	}
	slices.Sort(all)
	slices.Sort(fitting)
	refuse := func(r Refusal, names []string) StartFacts {
		return StartFacts{Refused: &PlaceRefusal{Refusal: r, Place: chosen, Need: need, Places: names}}
	}
	switch {
	case chosen != "" && named == nil:
		return refuse(RefuseEvalPlaceNotFound, all)
	case chosen != "" && need != nil && !named.Fits:
		return refuse(RefuseEvalPlaceMisfit, fitting)
	case chosen != "":
		return StartFacts{Card: card, EvalPlace: chosen}
	case !accelerator:
		return StartFacts{Card: card}
	case len(fitting) == 0:
		return refuse(RefuseEvalPlaceMisfit, nil)
	case len(fitting) > 1:
		return refuse(RefuseEvalPlaceAmbiguous, fitting)
	}
	return StartFacts{Card: card, EvalPlace: fitting[0]}
}

// startFacts is what a StartRun records. A client's chosen place is only ever kept as the shell
// resolved it: a start whose facts name another place than the one chosen was not resolved by a
// shell that held it, and is refused as a place not found rather than recorded as a guess.
func startFacts(b StartRun) StartFacts {
	f := b.Facts
	if f.Refused == nil && b.EvalPlace != "" && f.EvalPlace != b.EvalPlace {
		return StartFacts{Refused: &PlaceRefusal{Refusal: RefuseEvalPlaceNotFound, Place: b.EvalPlace}}
	}
	return f
}
