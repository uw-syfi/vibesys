package main

import "testing"

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

	if checkScenarioHistory(scenarioSPSC, 1, history) {
		t.Fatal("exact SPSC model accepted FULL and EMPTY around an unpublished enqueue")
	}
	if !checkScenarioHistory(scenarioMPMC, 1, history) {
		t.Fatal("reservation-aware MPMC model rejected a reserved but unpublished enqueue")
	}
}
