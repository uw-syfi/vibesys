package main

import "testing"

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
	if !checkExactFIFOHistory(2, history) {
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
			if checkExactFIFOHistory(1, history) {
				t.Fatal("invalid history was accepted")
			}
		})
	}
}
