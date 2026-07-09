package main

import (
	"os"
	"path/filepath"
	"testing"
)

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

func TestQueueModelAcceptsBoundedFIFOHistory(t *testing.T) {
	history := []recordedOperation{
		dequeueRecord(0, nil, 1),
		enqueueRecord(0, 10, true, 3),
		enqueueRecord(0, 20, true, 5),
		enqueueRecord(0, 30, false, 7),
		dequeueRecord(0, value(10), 9),
		dequeueRecord(0, value(20), 11),
		dequeueRecord(0, nil, 13),
	}
	if !isLinearizable(2, history) {
		t.Fatal("valid bounded FIFO history was rejected")
	}
}

func TestQueueModelRejectsIncorrectHistories(t *testing.T) {
	tests := map[string][]recordedOperation{
		"fifo violation": {
			enqueueRecord(0, 10, true, 1),
			enqueueRecord(0, 20, true, 3),
			dequeueRecord(0, value(20), 5),
		},
		"fabricated value": {
			dequeueRecord(0, value(99), 1),
		},
		"duplicate value": {
			enqueueRecord(0, 10, true, 1),
			dequeueRecord(0, value(10), 3),
			dequeueRecord(0, value(10), 5),
		},
		"capacity overflow": {
			enqueueRecord(0, 10, true, 1),
			enqueueRecord(0, 20, true, 3),
		},
		"early full response": {
			enqueueRecord(0, 10, false, 1),
		},
	}

	for name, history := range tests {
		t.Run(name, func(t *testing.T) {
			if isLinearizable(1, history) {
				t.Fatal("invalid history was accepted")
			}
		})
	}
}

func TestWorkerCountsEnforceScenarioShape(t *testing.T) {
	tests := []struct {
		scenario               scenario
		requestedP, requestedC int
		wantP, wantC           int
	}{
		{scenarioSPSC, 8, 8, 1, 1},
		{scenarioMPSC, 3, 8, 3, 1},
		{scenarioMPMC, 3, 2, 3, 2},
	}
	for _, test := range tests {
		producers, consumers, err := workerCounts(
			test.scenario,
			test.requestedP,
			test.requestedC,
		)
		if err != nil {
			t.Fatal(err)
		}
		if producers != test.wantP || consumers != test.wantC {
			t.Fatalf(
				"%s counts = %dP/%dC, want %dP/%dC",
				test.scenario,
				producers,
				consumers,
				test.wantP,
				test.wantC,
			)
		}
	}
}

func TestAccuracyCapacitiesIncludeContentionCases(t *testing.T) {
	capacities := accuracyCapacities(1024)
	want := []uint64{1024, 1, 2, 3}
	if len(capacities) != len(want) {
		t.Fatalf("capacities = %v, want %v", capacities, want)
	}
	for index := range want {
		if capacities[index] != want[index] {
			t.Fatalf("capacities = %v, want %v", capacities, want)
		}
	}

	capacities = accuracyCapacities(2)
	if len(capacities) != 3 {
		t.Fatalf("configured contention capacity was duplicated: %v", capacities)
	}
}

func TestQueueOutputRejectsWrongOperationStatus(t *testing.T) {
	_, err := queueOutputFor(
		request{operation: operationEnqueue, value: 1},
		response{status: statusValue, value: 1},
	)
	if err == nil {
		t.Fatal("enqueue accepted a dequeue response status")
	}

	_, err = queueOutputFor(
		request{operation: operationDequeue},
		response{status: statusEnqueued},
	)
	if err == nil {
		t.Fatal("dequeue accepted an enqueue response status")
	}
}

func TestAccuracyUsesFixedExternalLauncherContract(t *testing.T) {
	harnessSource, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	workspace := t.TempDir()
	launcher := filepath.Join(workspace, "queue-candidate")
	script := "#!/bin/sh\n" +
		"exec go -C \"$VSQ_HARNESS_SOURCE\" run . serve-reference \"$@\"\n"
	if err := os.WriteFile(launcher, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("VSQ_HARNESS_SOURCE", harnessSource)

	err = runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace: workspace,
			candidate: "queue-candidate",
			scenario:  scenarioMPSC,
			capacity:  4,
		},
		operations: 16,
		trials:     1,
		producers:  2,
		consumers:  1,
		seed:       7,
	})
	if err != nil {
		t.Fatal(err)
	}
}
