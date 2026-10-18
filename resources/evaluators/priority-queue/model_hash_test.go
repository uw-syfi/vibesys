package main

import (
	"testing"

	"github.com/anishathalye/porcupine"
)

// maxSampledStates bounds the pairs memoCost compares when it checks that equal states hash equal.
const maxSampledStates = 400

// memoCost decides operations against model and reports how many state comparisons the checker made
// per transition it tried, and whether two states the model considers equal hash differently. The
// counts are a function of the history alone, so they do not depend on how fast the machine is.
func memoCost(model porcupine.Model, operations []porcupine.Operation) (steps, comparisons int, inconsistent bool) {
	var reached []any
	step, equal := model.Step, model.Equal
	model.Step = func(state, input, output any) (bool, any) {
		steps++
		ok, next := step(state, input, output)
		if ok {
			reached = append(reached, next)
		}
		return ok, next
	}
	model.Equal = func(first, second any) bool {
		comparisons++
		return equal(first, second)
	}
	porcupine.CheckOperations(model, operations)
	// Checking every pair is quadratic, so sample the states evenly.
	stride := len(reached)/maxSampledStates + 1
	var sampled []any
	for index := 0; index < len(reached); index += stride {
		sampled = append(sampled, reached[index])
	}
	for index, first := range sampled {
		for _, second := range sampled[index+1:] {
			if equal(first, second) && model.Hash(first) != model.Hash(second) {
				inconsistent = true
			}
		}
	}
	return steps, comparisons, inconsistent
}

// unobservableRead holds count overlapping enqueues followed by a dequeue of a value nobody
// enqueued. The history is rejected, and only after the checker has tried every publication order of
// the enqueues, so the cost is the number of states the search must tell apart.
func unobservableRead(count int) []recordedOperation {
	history := make([]recordedOperation, 0, count+1)
	for index := 0; index < count; index++ {
		enqueue := enqueueRecord(index, uint64(index+1), true, int64(index+1))
		enqueue.Return = int64(100 + index)
		history = append(history, enqueue)
	}
	return append(history, dequeueRecord(count, value(uint64(count+1)), int64(1000)))
}

func TestModelsMemoizeStatesWithoutComparingAgainstEveryOne(t *testing.T) {
	for count := 5; count <= 7; count++ {
		history := unobservableRead(count)
		reservation, ok := reservationAwareOperations(history)
		if !ok {
			t.Fatal("the generated history is malformed")
		}
		exact := make([]porcupine.Operation, 0, len(history))
		for _, op := range history {
			exact = append(exact, op.porcupine())
		}
		models := map[string]struct {
			model      porcupine.Model
			operations []porcupine.Operation
		}{
			"reservation": {reservationAwarePriorityModel(count), reservation},
			"exact":       {priorityModel(count), exact},
		}

		for name, tc := range models {
			steps, comparisons, inconsistent := memoCost(tc.model, tc.operations)
			if inconsistent {
				t.Errorf("%s model: equal states hash differently", name)
			}
			if comparisons > steps {
				t.Errorf("%s model, %d overlapping writes: %d state comparisons for %d steps; "+
					"a missing Hash makes each lookup scan every state of a linearized set",
					name, count, comparisons, steps)
			}
		}
	}
}
