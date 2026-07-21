package main

import "testing"

func TestReservationAwareModelRejectsInvalidHistories(t *testing.T) {
	tests := map[string][]recordedOperation{
		"full without used capacity": {
			enqueueRecord(0, 10, false, 1),
		},
		"empty with published item": {
			enqueueRecord(0, 10, true, 1),
			dequeueRecord(1, nil, 3),
		},
		"fifo violation": {
			enqueueRecord(0, 10, true, 1),
			enqueueRecord(1, 20, true, 3),
			dequeueRecord(2, value(20), 5),
		},
		"fabricated value": {
			dequeueRecord(0, value(99), 1),
		},
		"duplicate value": {
			enqueueRecord(0, 10, true, 1),
			dequeueRecord(1, value(10), 3),
			dequeueRecord(2, value(10), 5),
		},
	}

	for name, history := range tests {
		t.Run(name, func(t *testing.T) {
			if checkReservationAwareFIFOHistory(1, history) {
				t.Fatal("invalid reservation-aware history was accepted")
			}
		})
	}
}

func TestReservationAwareFullCountsAllUsedCapacity(t *testing.T) {
	first := enqueueRecord(0, 10, true, 1)
	first.Return = 8
	full := enqueueRecord(1, 20, false, 3)
	history := []recordedOperation{first, full}

	if checkReservationAwareFIFOHistory(2, history) {
		t.Fatal("FULL was accepted with one reservation and two slots of capacity")
	}

	published := enqueueRecord(0, 5, true, 1)
	reserved := enqueueRecord(1, 10, true, 3)
	reserved.Return = 10
	full = enqueueRecord(2, 20, false, 5)
	if !checkReservationAwareFIFOHistory(
		2,
		[]recordedOperation{published, reserved, full},
	) {
		t.Fatal("FULL was rejected when published plus reserved items reached capacity")
	}
}
