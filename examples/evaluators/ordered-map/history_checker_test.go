package main

import (
	"testing"

	"github.com/anishathalye/porcupine"
)

func orderedMapAccepts(t *testing.T, history []recordedOperation) bool {
	t.Helper()
	return decidedVerdict(t, checkOrderedMapHistory(history, defaultCheckBudget))
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

func putRecord(client int, key, value string, call int64) recordedOperation {
	return recordedOperation{
		ClientID: client,
		Input:    mapInput{Kind: "put", Key: []byte(key), Value: []byte(value)},
		Call:     call,
		Output:   mapOutput{Status: statusOK},
		Return:   call + 1,
	}
}

func minRecord(client int, key, value string, call int64) recordedOperation {
	return recordedOperation{
		ClientID: client,
		Input:    mapInput{Kind: "min"},
		Call:     call,
		Output: mapOutput{
			Status: statusOK,
			Key:    []byte(key),
			Value:  []byte(value),
		},
		Return: call + 1,
	}
}

func rangeRecord(client int, start, end string, maxItems uint32, items []rangePair, remaining bool, call int64) recordedOperation {
	return recordedOperation{
		ClientID: client,
		Input: mapInput{
			Kind:     "range",
			Key:      []byte(start),
			Value:    []byte(end),
			MaxItems: maxItems,
		},
		Call: call,
		Output: mapOutput{
			Status:    statusOK,
			Items:     items,
			Remaining: remaining,
		},
		Return: call + 1,
	}
}

func pair(key, value string) rangePair {
	return rangePair{Key: []byte(key), Value: []byte(value)}
}

func TestOrderedMapModelAcceptsSequentialRangeSnapshot(t *testing.T) {
	history := []recordedOperation{
		putRecord(0, "a", "1", 1),
		putRecord(0, "c", "3", 3),
		rangeRecord(0, "a", "d", 4, []rangePair{pair("a", "1"), pair("c", "3")}, false, 5),
	}
	if !orderedMapAccepts(t, history) {
		t.Fatal("valid sequential range snapshot was rejected")
	}
}

func TestOrderedMapModelRejectsRangeThatDropsPresentKey(t *testing.T) {
	history := []recordedOperation{
		putRecord(0, "b", "2", 1),
		rangeRecord(0, "a", "d", 4, nil, false, 3),
	}
	if orderedMapAccepts(t, history) {
		t.Fatal("range that omitted a completed put was accepted")
	}
}

func TestOrderedMapModelRejectsMinThatSkipsLeastKey(t *testing.T) {
	history := []recordedOperation{
		putRecord(0, "a", "1", 1),
		putRecord(0, "c", "3", 3),
		minRecord(0, "c", "3", 5),
	}
	if orderedMapAccepts(t, history) {
		t.Fatal("min that skipped the least key was accepted")
	}
}

func TestOrderedMapModelRejectsOutOfOrderRangeItems(t *testing.T) {
	history := []recordedOperation{
		putRecord(0, "a", "1", 1),
		putRecord(0, "c", "3", 3),
		rangeRecord(0, "a", "d", 4, []rangePair{pair("c", "3"), pair("a", "1")}, false, 5),
	}
	if orderedMapAccepts(t, history) {
		t.Fatal("out-of-order range items were accepted")
	}
}

func TestOrderedMapModelRejectsWrongRangeRemaining(t *testing.T) {
	history := []recordedOperation{
		putRecord(0, "a", "1", 1),
		putRecord(0, "c", "3", 3),
		rangeRecord(0, "a", "d", 1, []rangePair{pair("a", "1")}, false, 5),
	}
	if orderedMapAccepts(t, history) {
		t.Fatal("range with a wrong remaining flag was accepted")
	}
}

func TestConcurrentModelAcceptsOverlappingIncompleteRange(t *testing.T) {
	put := putRecord(0, "b", "2", 1)
	put.Return = 10
	rng := rangeRecord(1, "a", "d", 4, nil, false, 2)
	rng.Return = 3
	rng.Input.Weak = true
	if !orderedMapAccepts(t, []recordedOperation{put, rng}) {
		t.Fatal("weak overlapping range that missed a concurrent put was rejected")
	}
}

func TestConcurrentModelAcceptsOverlappingMinThatSkipsLeastKey(t *testing.T) {
	first := putRecord(0, "a", "1", 1)
	first.Return = 10
	second := putRecord(0, "c", "3", 3)
	min := minRecord(1, "c", "3", 4)
	min.Return = 6
	min.Input.Weak = true
	if !orderedMapAccepts(t, []recordedOperation{first, second, min}) {
		t.Fatal("weak overlapping min that skipped the least key was rejected")
	}
}

func TestConcurrentModelRejectsUnsortedWeakRange(t *testing.T) {
	put := putRecord(0, "a", "1", 1)
	put.Return = 10
	rng := rangeRecord(1, "a", "d", 4, []rangePair{pair("c", "3"), pair("a", "1")}, false, 2)
	rng.Return = 3
	rng.Input.Weak = true
	if orderedMapAccepts(t, []recordedOperation{put, rng}) {
		t.Fatal("weak range with out-of-order items was accepted")
	}
}

func TestConcurrentModelRejectsWeakRangeOutsideInterval(t *testing.T) {
	put := putRecord(0, "z", "9", 1)
	put.Return = 10
	rng := rangeRecord(1, "a", "d", 4, []rangePair{pair("z", "9")}, false, 2)
	rng.Return = 3
	rng.Input.Weak = true
	if orderedMapAccepts(t, []recordedOperation{put, rng}) {
		t.Fatal("weak range item outside [start, end) was accepted")
	}
}
