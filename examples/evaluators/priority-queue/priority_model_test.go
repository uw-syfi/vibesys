package main

import "testing"

func TestPriorityModelAcceptsBoundedHistory(t *testing.T) {
	history := []recordedOperation{
		dequeueRecord(0, nil, 1),
		enqueueRecord(0, 10, true, 3),
		enqueueRecord(0, 20, true, 5),
		enqueueRecord(0, 30, false, 7),
		dequeueRecord(0, value(10), 9),
		dequeueRecord(0, value(20), 11),
		dequeueRecord(0, nil, 13),
	}
	if !checkPriorityHistory(2, history) {
		t.Fatal("valid bounded priority history was rejected")
	}
}

func TestPriorityModelRejectsIncorrectHistories(t *testing.T) {
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
			if checkPriorityHistory(1, history) {
				t.Fatal("invalid history was accepted")
			}
		})
	}
}

func TestPriorityModelAcceptsDuplicatePayloads(t *testing.T) {
	history := []recordedOperation{
		enqueueRecord(0, 10, true, 1),
		enqueueRecord(0, 10, true, 3),
		dequeueRecord(0, value(10), 5),
		dequeueRecord(0, value(10), 7),
	}
	if !checkPriorityHistory(2, history) {
		t.Fatal("valid duplicate payloads were rejected")
	}
}

func TestPriorityModelAcceptsEitherOrderForEqualPriorities(t *testing.T) {
	history := []recordedOperation{
		priorityEnqueueRecord(0, 10, 1, true, 1),
		priorityEnqueueRecord(0, 20, 1, true, 3),
		dequeueRecord(0, value(20), 5),
		dequeueRecord(0, value(10), 7),
	}
	if !checkPriorityHistory(2, history) {
		t.Fatal("valid equal-priority reordering was rejected")
	}
}

func TestPriorityModelRejectsPriorityViolation(t *testing.T) {
	history := []recordedOperation{
		priorityEnqueueRecord(0, 10, 9, true, 1),
		priorityEnqueueRecord(0, 20, 1, true, 3),
		dequeueRecord(0, value(10), 5),
	}
	if checkPriorityHistory(2, history) {
		t.Fatal("dequeue of a lower-ranked item was accepted")
	}
}

func TestPriorityModelRejectsMalformedHistories(t *testing.T) {
	ok := true
	payload := uint64(10)
	priority := uint64(1)
	tests := map[string][]recordedOperation{
		"enqueue without value": {{
			Input:  queueInput{Kind: "enqueue"},
			Output: queueOutput{EnqueueOK: &ok},
		}},
		"enqueue without result": {{
			Input: queueInput{Kind: "enqueue", Value: &payload, Priority: &priority},
		}},
		"enqueue without priority": {{
			Input:  queueInput{Kind: "enqueue", Value: &payload},
			Output: queueOutput{EnqueueOK: &ok},
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
			if checkPriorityHistory(1, history) {
				t.Fatal("malformed history was accepted")
			}
		})
	}
}
