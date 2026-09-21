package main

import "testing"

func TestStackModelAcceptsBoundedHistory(t *testing.T) {
	history := []recordedOperation{
		dequeueRecord(0, nil, 1),
		enqueueRecord(0, 10, true, 3),
		enqueueRecord(0, 20, true, 5),
		enqueueRecord(0, 30, false, 7),
		dequeueRecord(0, value(20), 9),
		dequeueRecord(0, value(10), 11),
		dequeueRecord(0, nil, 13),
	}
	if !checkStackHistory(2, history) {
		t.Fatal("valid bounded stack history was rejected")
	}
}

func TestStackModelRejectsIncorrectHistories(t *testing.T) {
	tests := map[string][]recordedOperation{
		"fabricated value": {
			dequeueRecord(0, value(99), 1),
		},
		"duplicate dequeue": {
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
			if checkStackHistory(1, history) {
				t.Fatal("invalid history was accepted")
			}
		})
	}
}

func TestStackModelAcceptsDuplicatePayloads(t *testing.T) {
	history := []recordedOperation{
		enqueueRecord(0, 10, true, 1),
		enqueueRecord(0, 10, true, 3),
		dequeueRecord(0, value(10), 5),
		dequeueRecord(0, value(10), 7),
	}
	if !checkStackHistory(2, history) {
		t.Fatal("valid duplicate payloads were rejected")
	}
}

func TestStackModelRequiresLIFOOrder(t *testing.T) {
	history := []recordedOperation{
		enqueueRecord(0, 10, true, 1),
		enqueueRecord(0, 20, true, 3),
		dequeueRecord(0, value(20), 5),
		dequeueRecord(0, value(10), 7),
	}
	if !checkStackHistory(2, history) {
		t.Fatal("valid LIFO history was rejected")
	}
}

func TestStackModelRejectsNonTopPop(t *testing.T) {
	history := []recordedOperation{
		enqueueRecord(0, 10, true, 1),
		enqueueRecord(0, 20, true, 3),
		dequeueRecord(0, value(10), 5),
	}
	if checkStackHistory(2, history) {
		t.Fatal("dequeue of a non-top item was accepted")
	}
}

func TestStackModelRejectsNonTopDuplicateValue(t *testing.T) {
	history := []recordedOperation{
		enqueueRecord(0, 10, true, 1),
		enqueueRecord(0, 20, true, 3),
		enqueueRecord(0, 10, true, 5),
		dequeueRecord(0, value(10), 7),
		dequeueRecord(0, value(10), 9),
	}
	if checkStackHistory(3, history) {
		t.Fatal("pop of a non-top duplicate payload was accepted")
	}
}

func TestStackModelRejectsMalformedHistories(t *testing.T) {
	ok := true
	payload := uint64(10)
	tests := map[string][]recordedOperation{
		"enqueue without value": {{
			Input:  queueInput{Kind: "enqueue"},
			Output: queueOutput{EnqueueOK: &ok},
		}},
		"enqueue without result": {{
			Input: queueInput{Kind: "enqueue", Value: &payload},
		}},
		"contradictory dequeue": {{
			Input:  queueInput{Kind: "dequeue"},
			Output: queueOutput{DequeueNone: true, DequeueVal: &payload},
		}},
		"unknown operation": {{
			Input: queueInput{Kind: "peek"},
		}},
	}

	for name, history := range tests {
		t.Run(name, func(t *testing.T) {
			if checkStackHistory(1, history) {
				t.Fatal("malformed history was accepted")
			}
		})
	}
}
