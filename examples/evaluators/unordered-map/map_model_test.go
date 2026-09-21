package main

import (
	"testing"

	"github.com/anishathalye/porcupine"
)

func putRecord(client int, key, value string, call int64) recordedOperation {
	return recordedOperation{
		ClientID: client,
		Input:    mapInput{Kind: "put", Key: key, Value: []byte(value)},
		Call:     call,
		Output:   mapOutput{Ok: true},
		Return:   call + 1,
	}
}

func getRecord(client int, key string, value *string, call int64) recordedOperation {
	output := mapOutput{Missing: value == nil}
	if value != nil {
		output.Ok = true
		output.Value = []byte(*value)
	}
	return recordedOperation{
		ClientID: client,
		Input:    mapInput{Kind: "get", Key: key},
		Call:     call,
		Output:   output,
		Return:   call + 1,
	}
}

func removeRecord(client int, key string, value *string, call int64) recordedOperation {
	output := mapOutput{Missing: value == nil}
	if value != nil {
		output.Ok = true
		output.Value = []byte(*value)
	}
	return recordedOperation{
		ClientID: client,
		Input:    mapInput{Kind: "remove", Key: key},
		Call:     call,
		Output:   output,
		Return:   call + 1,
	}
}

func text(value string) *string {
	return &value
}

func historyOk(history []recordedOperation) bool {
	return checkMapHistory(history, defaultCheckBudget) == porcupine.Ok
}

func TestMapModelAcceptsValidHistory(t *testing.T) {
	history := []recordedOperation{
		getRecord(0, "a", nil, 1),
		putRecord(0, "a", "v1", 3),
		getRecord(0, "a", text("v1"), 5),
		putRecord(0, "a", "v2", 7),
		getRecord(0, "a", text("v2"), 9),
		removeRecord(0, "a", text("v2"), 11),
		getRecord(0, "a", nil, 13),
		removeRecord(0, "a", nil, 15),
		putRecord(0, "b", "v3", 17),
		getRecord(0, "b", text("v3"), 19),
	}
	if !historyOk(history) {
		t.Fatal("valid unordered-map history was rejected")
	}
}

func TestMapModelRejectsStaleGet(t *testing.T) {
	history := []recordedOperation{
		putRecord(0, "a", "v1", 1),
		putRecord(0, "a", "v2", 3),
		getRecord(0, "a", text("v1"), 5),
	}
	if historyOk(history) {
		t.Fatal("stale get was accepted")
	}
}

func TestMapModelRejectsLostPut(t *testing.T) {
	history := []recordedOperation{
		putRecord(0, "a", "v1", 1),
		getRecord(0, "a", nil, 3),
	}
	if historyOk(history) {
		t.Fatal("lost put was accepted")
	}
}

func TestMapModelRejectsRemoveOfMissingThatReturnsOK(t *testing.T) {
	history := []recordedOperation{
		removeRecord(0, "a", text("ghost"), 1),
	}
	if historyOk(history) {
		t.Fatal("remove of a missing key that returned OK was accepted")
	}
}

func TestMapModelAcceptsConcurrentOpsOnDifferentKeys(t *testing.T) {
	history := []recordedOperation{
		{
			ClientID: 0,
			Input:    mapInput{Kind: "put", Key: "a", Value: []byte("v1")},
			Call:     1,
			Output:   mapOutput{Ok: true},
			Return:   4,
		},
		{
			ClientID: 1,
			Input:    mapInput{Kind: "put", Key: "b", Value: []byte("v2")},
			Call:     2,
			Output:   mapOutput{Ok: true},
			Return:   3,
		},
		getRecord(0, "a", text("v1"), 5),
		getRecord(1, "b", text("v2"), 7),
	}
	if !historyOk(history) {
		t.Fatal("linearizable history on commuting keys was rejected")
	}
}

func TestMapModelPartitionsByKey(t *testing.T) {
	history := []recordedOperation{
		putRecord(0, "a", "v1", 1),
		putRecord(1, "b", "v2", 3),
		getRecord(0, "a", text("v1"), 5),
	}
	operations := make([]porcupine.Operation, 0, len(history))
	for _, op := range history {
		operations = append(operations, op.porcupine())
	}
	partitions := mapModel().Partition(operations)
	if len(partitions) != 2 {
		t.Fatalf("partition count = %d, want 2 (one per key)", len(partitions))
	}
	if len(partitions[0]) == 0 || len(partitions[1]) == 0 {
		t.Fatal("a key partition was empty")
	}
}

func TestMapModelRejectsMalformedHistories(t *testing.T) {
	payload := []byte("v1")
	tests := map[string][]recordedOperation{
		"unknown operation": {{
			Input: mapInput{Kind: "cas", Key: "a"},
		}},
		"put that reported missing": {{
			Input:  mapInput{Kind: "put", Key: "a", Value: payload},
			Output: mapOutput{Missing: true},
		}},
		"get that reported both ok and missing": {{
			Input:  mapInput{Kind: "get", Key: "a"},
			Output: mapOutput{Ok: true, Missing: true, Value: payload},
		}},
		"remove of present without value": {
			putRecord(0, "a", "v1", 1),
			{
				ClientID: 0,
				Input:    mapInput{Kind: "remove", Key: "a"},
				Call:     3,
				Output:   mapOutput{Ok: true},
				Return:   4,
			},
		},
	}

	for name, history := range tests {
		t.Run(name, func(t *testing.T) {
			if historyOk(history) {
				t.Fatal("malformed history was accepted")
			}
		})
	}
}
