package main

import (
	"testing"

	"github.com/anishathalye/porcupine"
)

// The fixtures in these model tests hold a handful of operations, so the check
// budget is never the limiting factor and the verdict is effectively boolean.
// An undecided verdict means the fixture, not the model, is wrong, so these
// helpers fail the test rather than folding Unknown into either answer.

func exactFIFOAccepts(t *testing.T, capacity int, history []recordedOperation) bool {
	t.Helper()
	return decidedVerdict(t, checkExactFIFOHistory(capacity, history, defaultCheckBudget))
}

func reservationFIFOAccepts(t *testing.T, capacity int, history []recordedOperation) bool {
	t.Helper()
	return decidedVerdict(
		t,
		checkReservationAwareFIFOHistory(capacity, history, defaultCheckBudget),
	)
}

func scenarioAccepts(
	t *testing.T,
	s scenario,
	capacity int,
	history []recordedOperation,
) bool {
	t.Helper()
	return decidedVerdict(t, checkScenarioHistory(s, capacity, history, defaultCheckBudget))
}

func decidedVerdict(t *testing.T, verdict porcupine.CheckResult) bool {
	t.Helper()
	switch verdict {
	case porcupine.Ok:
		return true
	case porcupine.Illegal:
		return false
	default:
		t.Fatalf("fixture history was undecided within %s: %s", defaultCheckBudget, verdict)
		return false
	}
}

func enqueueRecord(client int, value uint64, ok bool, call int64) recordedOperation {
	return recordedOperation{
		ClientID: client,
		Input:    queueInput{Kind: "enqueue", Value: &value},
		Call:     call,
		Output:   queueOutput{EnqueueOK: &ok},
		Return:   call + 1,
	}
}

func dequeueRecord(client int, value *uint64, call int64) recordedOperation {
	output := queueOutput{DequeueNone: value == nil}
	if value != nil {
		copy := *value
		output.DequeueVal = &copy
	}
	return recordedOperation{
		ClientID: client,
		Input:    queueInput{Kind: "dequeue"},
		Call:     call,
		Output:   output,
		Return:   call + 1,
	}
}

func value(value uint64) *uint64 {
	return &value
}

func TestScenarioModelsApplyDistinctReservationSemantics(t *testing.T) {
	first := enqueueRecord(0, 10, true, 1)
	first.Return = 8
	full := enqueueRecord(1, 20, false, 3)
	empty := dequeueRecord(2, nil, 5)
	history := []recordedOperation{first, full, empty}

	if scenarioAccepts(t, scenarioSPSC, 1, history) {
		t.Fatal("exact SPSC model accepted FULL and EMPTY around an unpublished enqueue")
	}
	if scenarioAccepts(t, scenarioMPSC, 1, history) {
		t.Fatal("exact MPSC model accepted FULL and EMPTY around an unpublished enqueue")
	}
	if !scenarioAccepts(t, scenarioMPMC, 1, history) {
		t.Fatal("reservation-aware MPMC model rejected a reserved but unpublished enqueue")
	}
}

func TestModelsAgreeOnSequentialHistories(t *testing.T) {
	tests := map[string][]recordedOperation{
		"valid bounded fifo": {
			enqueueRecord(0, 10, true, 1),
			enqueueRecord(0, 20, false, 3),
			dequeueRecord(0, value(10), 5),
			dequeueRecord(0, nil, 7),
		},
		"duplicate payloads": {
			enqueueRecord(0, 10, true, 1),
			dequeueRecord(0, value(10), 3),
			enqueueRecord(0, 10, true, 5),
			dequeueRecord(0, value(10), 7),
		},
		"false full": {
			enqueueRecord(0, 10, false, 1),
		},
		"false empty": {
			enqueueRecord(0, 10, true, 1),
			dequeueRecord(0, nil, 3),
		},
		"fifo violation": {
			enqueueRecord(0, 10, true, 1),
			enqueueRecord(0, 20, true, 3),
			dequeueRecord(0, value(20), 5),
		},
	}

	for name, history := range tests {
		t.Run(name, func(t *testing.T) {
			exact := exactFIFOAccepts(t, 1, history)
			reservationAware := reservationFIFOAccepts(t, 1, history)
			if exact != reservationAware {
				t.Fatalf(
					"sequential verdicts differ: exact=%t reservation-aware=%t",
					exact,
					reservationAware,
				)
			}
		})
	}
}

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
	porcupine.CheckOperationsTimeout(model, operations, defaultCheckBudget)
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
			"reservation": {reservationAwareFIFOModel(count), reservation},
			"exact":       {exactFIFOModel(count), exact},
		}
		for name, tc := range models {
			steps, comparisons, inconsistent := memoCost(tc.model, tc.operations)
			if inconsistent {
				t.Errorf("%s model: equal states hash differently", name)
			}
			if comparisons > steps {
				t.Errorf("%s model, %d enqueues: %d state comparisons for %d steps; "+
					"a missing Hash makes each lookup scan every state of a linearized set",
					name, count, comparisons, steps)
			}
		}
	}
}
