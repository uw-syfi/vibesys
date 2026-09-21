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

func TestScenarioModelRequiresLIFOOrder(t *testing.T) {
	history := []recordedOperation{
		enqueueRecord(0, 10, true, 1),
		enqueueRecord(0, 20, true, 3),
		enqueueRecord(0, 30, true, 5),
		dequeueRecord(0, value(30), 7),
		dequeueRecord(0, value(20), 9),
		dequeueRecord(0, value(10), 11),
	}
	if !checkScenarioHistory(scenarioSPSC, 3, history) {
		t.Fatal("valid LIFO history was rejected")
	}
}

func TestModelsAgreeOnSequentialHistories(t *testing.T) {
	tests := map[string][]recordedOperation{
		"valid bounded stack": {
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
	}

	for name, history := range tests {
		t.Run(name, func(t *testing.T) {
			exact := checkStackHistory(1, history)
			scenario := checkScenarioHistory(scenarioSPSC, 1, history)
			if exact != scenario {
				t.Fatalf("model verdicts differ: exact=%t scenario=%t", exact, scenario)
			}
		})
	}
}

func TestReservationModelAllowsReservedCapacityToBeFullAndPublishedStackEmpty(t *testing.T) {
	history := []recordedOperation{
		enqueueRecord(0, 10, true, 1),
		enqueueRecord(1, 20, false, 2),
		dequeueRecord(2, nil, 4),
	}
	history[0].Return = 7
	if !checkScenarioHistory(scenarioMPMC, 1, history) {
		t.Fatal("valid reservation-aware overlap was rejected")
	}
	if checkStackHistory(1, history) {
		t.Fatal("strict model unexpectedly accepted reservation-aware overlap")
	}
}

func TestReservationModelPopsLastPublishedItem(t *testing.T) {
	history := []recordedOperation{
		enqueueRecord(0, 10, true, 1),
		enqueueRecord(1, 20, true, 3),
		dequeueRecord(2, value(10), 5),
	}
	if checkScenarioHistory(scenarioMPMC, 2, history) {
		t.Fatal("pop skipped the last published item")
	}
	history[2] = dequeueRecord(2, value(20), 5)
	if !checkScenarioHistory(scenarioMPMC, 2, history) {
		t.Fatal("last published item was rejected")
	}
}

func TestReservationModelRequiresLIFOOfPublishedItems(t *testing.T) {
	history := []recordedOperation{
		enqueueRecord(0, 10, true, 1),
		enqueueRecord(1, 20, true, 3),
		dequeueRecord(2, value(20), 5),
		dequeueRecord(3, value(10), 7),
	}
	if !checkScenarioHistory(scenarioMPMC, 2, history) {
		t.Fatal("valid published LIFO order was rejected")
	}
}

func TestReservationModelRejectsMissingValue(t *testing.T) {
	ok := true
	history := []recordedOperation{{
		Input: queueInput{Kind: "enqueue"},
		Call:  1, Output: queueOutput{EnqueueOK: &ok}, Return: 2,
	}}
	if checkScenarioHistory(scenarioMPMC, 1, history) {
		t.Fatal("enqueue without value was accepted")
	}
}
