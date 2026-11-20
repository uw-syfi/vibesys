package core

import (
	"fmt"
	"slices"
	"strings"
)

// PlanKind is what kind of change a plan makes, as its planner states it. The CLI stores it and
// filters by it, and decides nothing from it.
type PlanKind string

// The plan kinds. The zero value, "", is a plan whose planner states no kind.
const (
	// PlanKernel changes one kernel or operator.
	PlanKernel PlanKind = "kernel"
	// PlanSystem changes how the system runs work: scheduling, batching, memory management.
	PlanSystem PlanKind = "system"
	// PlanLayout changes how data is laid out in memory or across devices.
	PlanLayout PlanKind = "layout"
	// PlanSimulator builds or changes a model or simulator that predicts what other plans gain.
	PlanSimulator PlanKind = "simulator"
	// PlanProbe measures or profiles to answer a question, and changes nothing that ships.
	PlanProbe PlanKind = "probe"
	// PlanIntegration merges the work of other plans into one change.
	PlanIntegration PlanKind = "integration"
)

// PlanKinds lists every plan kind, in the order a listing names them.
func PlanKinds() []PlanKind {
	return []PlanKind{PlanKernel, PlanSystem, PlanLayout, PlanSimulator, PlanProbe, PlanIntegration}
}

// Valid says whether the kind is one of the six. The zero kind, none stated, is not one.
func (k PlanKind) Valid() bool { return slices.Contains(PlanKinds(), k) }

// NormalizeDependsOn returns the plan numbers ascending and each once, the form a store keeps.
func NormalizeDependsOn(deps []PlanNumber) []PlanNumber {
	out := slices.Clone(deps)
	slices.Sort(out)
	return slices.Compact(out)
}

// CheckDependsOn says whether plan number may depend on deps, among plans, every plan of the task
// with the dependencies it has: each of deps is a plan there (the error is a *Failure of kind
// FailureNotFound naming it, which a new plan's own number is too, as it is not there yet), none is
// the plan itself, and none depends on the plan, directly or
// through others, since a dependency never makes a cycle (FailureInvalidInput naming the cycle).
// The plan's own dependencies in plans are the ones deps replace, and play no part. The field
// names what the error is about.
func CheckDependsOn(field string, number PlanNumber, deps []PlanNumber, plans []Plan) error {
	edges := make(map[PlanNumber][]PlanNumber, len(plans))
	for _, p := range plans {
		edges[p.Number] = p.DependsOn
	}
	for _, d := range deps {
		if _, ok := edges[d]; !ok {
			return failf(FailureNotFound, "%s: this task has no plan %d; plan list shows the plans there are", field, d)
		}
		if d == number {
			return failf(FailureInvalidInput, "%s: plan %d cannot depend on itself", field, number)
		}
	}
	delete(edges, number)
	for _, d := range deps {
		if path := pathTo(d, number, edges, map[PlanNumber]bool{}); path != nil {
			steps := make([]string, 0, len(path)+1)
			steps = append(steps, fmt.Sprint(number))
			for _, p := range path {
				steps = append(steps, fmt.Sprint(p))
			}
			return failf(FailureInvalidInput, "%s: plan %d would depend on itself through %s; dependencies make no cycle",
				field, number, strings.Join(steps, " -> "))
		}
	}
	return nil
}

// pathTo returns a path of dependencies from one plan to another, both included, or nil when there
// is none. A plan that is not in edges (the target) has none of its own.
func pathTo(from, to PlanNumber, edges map[PlanNumber][]PlanNumber, seen map[PlanNumber]bool) []PlanNumber {
	if from == to {
		return []PlanNumber{to}
	}
	if seen[from] {
		return nil
	}
	seen[from] = true
	for _, next := range edges[from] {
		if rest := pathTo(next, to, edges, seen); rest != nil {
			return append([]PlanNumber{from}, rest...)
		}
	}
	return nil
}

// Ready says whether the plan is ready to start, among plans, every plan of the task: it is open,
// and each plan it depends on is won. A plan with no dependencies is ready while it is open. A
// dependency in any other status, chosen, parked or rejected included, keeps it waiting, since only
// a won plan has landed what this one builds on; a lead that drops a dependency edits the plan's
// dependencies. A dependency that is not among plans keeps it waiting too.
func (p Plan) Ready(plans []Plan) bool {
	if p.Status != PlanOpen {
		return false
	}
	for _, d := range p.DependsOn {
		i := slices.IndexFunc(plans, func(q Plan) bool { return q.Number == d })
		if i < 0 || plans[i].Status != PlanWon {
			return false
		}
	}
	return true
}

// Dependents returns the plans among plans that depend on number, in the order given.
func Dependents(number PlanNumber, plans []Plan) []Plan {
	var out []Plan
	for _, p := range plans {
		if slices.Contains(p.DependsOn, number) {
			out = append(out, p)
		}
	}
	return out
}
