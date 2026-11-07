package core

import (
	"math"
	"strings"
	"unicode/utf8"
)

// A bound is the performance bound of an objective as an agent declares it: the best the
// objective can reach, derived from first principles. The bounds of a task are an append-only log.
// A declaration is never changed or removed, and the one that stands for an objective is the
// newest, so the log keeps every version with who declared it and when. The framework compares
// nothing with a bound and requires none: it records what an agent states, checks it is well formed
// and about an objective the task has, and serves it.

// The limits on what a bound holds, which keep a runaway write from filling the store and a view
// of it within its byte budget.
const (
	MaxBoundObjectiveBytes = 256
	MaxBoundUnitBytes      = 64
	MaxBoundNoteBytes      = 4096
)

// NewBound is a decided declaration that has no place in the log yet.
type NewBound struct {
	Origin RecordOrigin
	// Objective is one the task declares.
	Objective string
	// Direction is which way the objective improves, as the task declares it: the bound is the most
	// a maximized objective can reach and the least a minimized one can.
	Direction Direction
	// Value is finite and above zero, in the unit of the objective's own measurements. The daemon
	// compares nothing with it; a reader that divides a measurement by it relies on that.
	Value float64
	// Unit is what Value is counted in, a label for people.
	Unit string
	// Note is how the bound was derived: the provenance a reader judges it by.
	Note string
}

// Bound is one declaration in a task's log of bounds.
type Bound struct {
	// Seq is the place in the log, 1 for the first. The store assigns it and never reuses it.
	Seq uint64
	NewBound
}

// BoundStatement is what an agent states when it declares a bound: the objective, the number, its
// unit and how it was derived. The direction is not the agent's to state, since the task has it.
type BoundStatement struct {
	Objective string
	Value     float64
	Unit      string
	Note      string
}

// DecideBound turns a statement into the declaration it makes, from the objectives the task
// declares. It fails with a *Failure of kind FailureInvalidInput that names the field: an objective
// the task does not declare, which lists the ones it does; a value that is not finite and above
// zero; a unit that is not one line of text; and a note that is empty or too long. Nothing else is
// decided: a bound that a measurement has already passed is the agent's to revise, not a refusal.
func DecideBound(objectives []Metric, s BoundStatement, origin RecordOrigin) (NewBound, error) {
	if err := checkBoundText("objective", s.Objective, MaxBoundObjectiveBytes); err != nil {
		return NewBound{}, err
	}
	var direction Direction
	names := make([]string, 0, len(objectives))
	for _, m := range objectives {
		names = append(names, `"`+m.Name+`"`)
		if m.Name == s.Objective {
			direction = m.Direction
		}
	}
	if direction == "" {
		return NewBound{}, failf(FailureInvalidInput, "objective: %q is not an objective of this task, which declares %s", s.Objective, strings.Join(names, ", "))
	}
	b := NewBound{Origin: origin, Objective: s.Objective, Direction: direction, Value: s.Value, Unit: strings.TrimSpace(s.Unit), Note: s.Note}
	return b, b.Check()
}

// Check says whether the bound is well formed, whatever task it is for: what a store holds. The
// error is a *Failure of kind FailureInvalidInput that names the field.
func (b NewBound) Check() error {
	if err := checkBoundText("objective", b.Objective, MaxBoundObjectiveBytes); err != nil {
		return err
	}
	switch {
	case b.Direction != Maximize && b.Direction != Minimize:
		return failf(FailureInvalidInput, "direction: %q is not a direction", b.Direction)
	case math.IsNaN(b.Value) || math.IsInf(b.Value, 0) || b.Value <= 0:
		return failf(FailureInvalidInput, "value: a bound is a finite number above 0, got %v", b.Value)
	case strings.ContainsAny(b.Unit, "\r\n"):
		return failf(FailureInvalidInput, "unit: a unit is one line")
	}
	if err := b.Origin.Check("origin"); err != nil {
		return err
	}
	if err := checkBoundText("unit", b.Unit, MaxBoundUnitBytes); err != nil {
		return err
	}
	return checkBoundText("note", b.Note, MaxBoundNoteBytes)
}

// checkBoundText says whether a text is valid UTF-8, not empty or blank, and within its limit.
func checkBoundText(field, text string, limit int) error {
	switch {
	case strings.TrimSpace(text) == "":
		return failf(FailureInvalidInput, "%s: give a non-empty %s", field, field)
	case len(text) > limit:
		return failf(FailureInvalidInput, "%s: %d bytes is over the limit of %d", field, len(text), limit)
	case !utf8.ValidString(text):
		return failf(FailureInvalidInput, "%s: not valid UTF-8", field)
	}
	return nil
}

// StandingBounds returns the bound that stands for each objective, the newest, in the order of
// the log, which is in the order of its places.
func StandingBounds(log []Bound) []Bound {
	newest := make(map[string]uint64, len(log))
	for _, b := range log {
		newest[b.Objective] = max(newest[b.Objective], b.Seq)
	}
	var out []Bound
	for _, b := range log {
		if newest[b.Objective] == b.Seq {
			out = append(out, b)
		}
	}
	return out
}
