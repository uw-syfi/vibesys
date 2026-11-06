// Package workload is the Go front end of vibesys-evaluator: a task's request generator written
// in Go. It has the concepts of the Python SDK's vibesys_evaluator.microservices: a Workload is a
// dataset, a model built from it, and a weighted mix of Operations; each Operation builds a
// Request (Steps from HTTP, or its shorthands Get, Post and Put; a Template filled from an
// earlier step's response; the info it was built from). The dataset and the model are any Go
// types, so a task's existing reference state serves as it is. Rand is bit-identical to the
// Python SDK's Rand.
//
// Main is the generator command that loadgen runs in the phase pod (see "Generator command
// line" in sdk/vibesys-evaluator/docs/spec.md): `generate` writes the request stream on standard
// output until loadgen closes it. For the same PhaseInput, a Go and a Python generator of the same
// workload write the same stream. A benchmark only measures: whether the responses are right is
// for an accuracy check to judge (package check), from a history the phase runner captures.
package workload

import (
	"fmt"
	"math"
)

// Workload is the dataset, the model the operations share, and the operations: the Python
// SDK's Workload[D, M]. D and M are any Go types the writer has.
type Workload[D, M any] struct {
	// Dataset loads the task's dataset from the file the orchestrator passes (--dataset: the
	// JSON that the Python dataset() encodes, see docs/spec.md); once per process, before the
	// model. Nil when the model needs no dataset (D's zero value is passed).
	Dataset func(path string) (D, error)
	// Model builds a phase's model from the dataset and the sample's model stream (the same in
	// every phase of a sample). Every phase starts from a model built afresh: what an earlier
	// phase's requests changed in the application is not in it.
	Model func(data D, rand *Rand) (M, error)
	// BeginPhase, when set, is called once per phase with the model and the phase's index (0 for
	// the sample's first phase), before the first Build: a workload whose requests depend on the
	// phase (an accuracy workload that reads in one phase what an earlier one wrote) keeps the
	// index in its model. An error fails the phase.
	BeginPhase func(model M, phase int) error
	// Operations is the mix; names are unique.
	Operations []Operation[M]
}

// Operation is one operation of the mix.
type Operation[M any] struct {
	// Name is unique in the workload: ^[a-z][a-z0-9_]{0,62}$.
	Name string
	// Weight is its share of the mix, relative to the other weights; > 0.
	Weight float64
	// Build builds one request from the model and the operation's own stream. It may change the
	// model, so that later operations build on it (an update, then a read of the new value): the
	// model is the phase's own, and Build runs for the operations in stream order, also for the
	// last ones generated ahead that loadgen never sends.
	Build func(model M, rand *Rand) Request
}

func (o Operation[M]) validate() error {
	if !validName(o.Name) {
		return fmt.Errorf("operation name %q must match %s", o.Name, namePattern)
	}
	if !(o.Weight > 0) || math.IsInf(o.Weight, 0) {
		return fmt.Errorf("operation %s: weight must be a finite number > 0, got %v", o.Name, o.Weight)
	}
	if o.Build == nil {
		return fmt.Errorf("operation %s: no Build", o.Name)
	}
	return nil
}

func (w Workload[D, M]) validate() error {
	if w.Model == nil {
		return fmt.Errorf("Workload has no Model")
	}
	if len(w.Operations) == 0 {
		return fmt.Errorf("Workload needs at least one operation")
	}
	seen := map[string]bool{}
	for _, o := range w.Operations {
		if err := o.validate(); err != nil {
			return err
		}
		if seen[o.Name] {
			return fmt.Errorf("Workload has two operations named %q", o.Name)
		}
		seen[o.Name] = true
	}
	return nil
}
