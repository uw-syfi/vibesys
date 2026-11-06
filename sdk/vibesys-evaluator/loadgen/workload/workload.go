// Package workload is the Go front end of vibesys-evaluator: a task's request generator written
// in Go. It has the concepts of the Python SDK's vibesys_evaluator.microservices: a Workload is a
// dataset, a model built from it, and a weighted mix of Operations; each Operation builds a
// Request (Steps from Get, Post and Put, a Template filled from an earlier step's response, the
// info it was built from, an exclusive key), a write's Apply updates the model, and Check says
// what is wrong with a response after the phase. The dataset and the model are any Go types, so a
// task's existing reference model and checks serve as they are. Rand is bit-identical to the
// Python SDK's Rand.
//
// Main is the generator command that loadgen and the phase pod run (see "Generator command
// line" in sdk/vibesys-evaluator/docs/spec.md): `generate` writes the request stream on standard
// output until loadgen closes it, and `check` checks the phase's results afterwards. For the same
// PhaseInput, a Go and a Python generator of the same workload write the same stream.
package workload

import (
	"encoding/json"
	"fmt"
	"math"
	"time"
)

// DefaultReadSample is the share of a read-only operation's responses that are checked when
// Operation.Sample is 0.
const DefaultReadSample = 0.1

// Workload is the dataset, the model the operations share, and the operations: the Python
// SDK's Workload[D, M]. D and M are any Go types the writer has: a task's existing reference
// model and its checks fit as they are.
type Workload[D, M any] struct {
	// Dataset loads the task's dataset from the file the orchestrator passes (--dataset: the
	// JSON that the Python dataset() encodes, see docs/spec.md); once per process, before the
	// model. Nil when the model needs no dataset (D's zero value is passed).
	Dataset func(path string) (D, error)
	// Model builds a phase's model from the dataset and the sample's model stream (the same in
	// every phase of a sample).
	Model func(data D, rand *Rand) (M, error)
	// Operations is the mix; names are unique.
	Operations []Operation[M]
}

// Operation is one operation of the mix.
type Operation[M any] struct {
	// Name is unique in the workload: ^[a-z][a-z0-9_]{0,62}$.
	Name string
	// Weight is its share of the mix, relative to the other weights; > 0.
	Weight float64
	// Build builds one request from the model and the operation's own stream.
	Build func(model M, rand *Rand) Request
	// Check says what is wrong with a response (nil when it is right), after the phase, over
	// the validation sample. Without it the operation is never checked.
	Check func(model M, call Call) error
	// Apply makes the operation a write: it records it in the model, pending while the phase
	// is generated and with its outcome afterwards. An error fails the phase.
	Apply func(model M, call Call) error
	// Sample is the share of operations checked, in (0, 1]; 0 means the default: 1 for writes
	// (which may not set less), DefaultReadSample for other operations.
	Sample float64
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
	if o.Sample < 0 || o.Sample > 1 || math.IsNaN(o.Sample) {
		return fmt.Errorf("operation %s: sample must be in [0, 1], got %v", o.Name, o.Sample)
	}
	if o.Apply != nil && o.Sample != 0 && o.Sample < 1 {
		return fmt.Errorf("operation %s: a write (it has Apply) is always checked; drop Sample %v", o.Name, o.Sample)
	}
	return nil
}

// checkedShare is the share of the operation's responses that are checked: 0 without a check.
func (o Operation[M]) checkedShare() float64 {
	switch {
	case o.Check == nil:
		return 0
	case o.Sample != 0:
		return o.Sample
	case o.Apply != nil:
		return 1
	default:
		return DefaultReadSample
	}
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

// Response is one step's response.
type Response struct {
	// Status is the HTTP status; 0 when no response arrived.
	Status int
	// Body is the body (operations in the validation sample keep it whole).
	Body []byte
}

// Call is one operation as sent: what it was built from, when it ran and what came back. While a
// phase is generated, a write's Apply sees it Pending: no times, OK false and no responses.
type Call struct {
	// Info is the Request's Info as JSON (a JSON object).
	Info json.RawMessage
	// Pending is true while the phase is generated.
	Pending bool
	// Start is when its first step was sent; End when its last response was complete.
	Start, End time.Time
	// OK is true when every step answered with an expected status.
	OK bool
	// Responses has one per step that was sent.
	Responses []Response
}

// DecodeInfo unmarshals the call's Info into v (encoding/json).
func (c Call) DecodeInfo(v any) error { return json.Unmarshal(c.Info, v) }

// Versions holds the values written to each key, with when, to say what a read may show: the
// Python SDK's Versions. A read that ran over [start, end] may show, per key, the latest value
// acknowledged more than the bound before it started (the default if there is none), or any value
// whose write started before the read ended and was not certainly overwritten before the read
// started. A write that failed counts as possibly applied, at any time after it started.
type Versions[K comparable, V comparable] struct {
	bound  time.Duration
	writes map[K][]write[V]
}

type write[V any] struct {
	value      V
	start, end int64
	pending    bool
	ok         bool
}

// NewVersions tracks the values of keys whose writes become visible within bound (>= 0).
func NewVersions[K comparable, V comparable](bound time.Duration) *Versions[K, V] {
	if bound < 0 {
		panic(fmt.Sprintf("workload.NewVersions: bound must be >= 0, got %v", bound))
	}
	return &Versions[K, V]{bound: bound, writes: map[K][]write[V]{}}
}

// Write records that call wrote value to key (in application order per key), pending or with
// its times and outcome.
func (v *Versions[K, V]) Write(key K, value V, call Call) {
	w := write[V]{value: value, pending: call.Pending, ok: call.OK}
	if !call.Pending {
		w.start, w.end = call.Start.UnixNano(), call.End.UnixNano()
	}
	v.writes[key] = append(v.writes[key], w)
}

// Latest is the last value written to key, pending writes included; def if none: what
// generation builds on.
func (v *Versions[K, V]) Latest(key K, def V) V {
	writes := v.writes[key]
	if len(writes) == 0 {
		return def
	}
	return writes[len(writes)-1].value
}

// Allowed is the values a read of key over [start, end] may show, oldest first, distinct; at
// least one. def is the key's value before any write (the seeded one).
func (v *Versions[K, V]) Allowed(key K, def V, start, end time.Time) []V {
	visibleBy := start.Add(-v.bound).UnixNano()
	settled, hasSettled := int64(0), false
	for _, w := range v.writes[key] {
		if !w.pending && w.ok && w.end <= visibleBy && (!hasSettled || w.start > settled) {
			settled, hasSettled = w.start, true
		}
	}
	var values []V
	if !hasSettled {
		values = append(values, def)
	}
	for _, w := range v.writes[key] {
		if w.pending || w.start > end.UnixNano() {
			continue
		}
		overwritten := hasSettled && w.ok && w.end <= settled
		if !overwritten && !contains(values, w.value) {
			values = append(values, w.value)
		}
	}
	return values
}

func contains[V comparable](values []V, value V) bool {
	for _, v := range values {
		if v == value {
			return true
		}
	}
	return false
}
