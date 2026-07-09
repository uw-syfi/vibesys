package main

import (
	"runtime"
	"testing"
)

func startInProcessCandidate(t *testing.T, capacity uint64) (*candidateSession, func()) {
	t.Helper()
	region, err := createRegion(scenarioSPSC, capacity, 1)
	if err != nil {
		t.Fatal(err)
	}
	serverDone := make(chan error, 1)
	go func() {
		serverDone <- serveReference(region.path)
	}()
	for !region.ready() {
		runtime.Gosched()
	}
	session := &candidateSession{
		region:    region,
		done:      make(chan struct{}),
		sequences: make([]uint64, 1),
		log:       newBoundedLog(1024),
	}
	cleanup := func() {
		region.stop()
		if err := <-serverDone; err != nil {
			t.Error(err)
		}
		if err := region.close(); err != nil {
			t.Error(err)
		}
	}
	return session, cleanup
}

func TestBenchmarkDrainValidatesSuccessfulOperationMultiset(t *testing.T) {
	session, cleanup := startInProcessCandidate(t, 4)
	defer cleanup()
	keys := fingerprintKeys{11, 29}
	counts := benchmarkCounts{enqueued: 2, dequeued: 1}
	addFingerprint(&counts.enqueuedFingerprint, keys, 10)
	addFingerprint(&counts.enqueuedFingerprint, keys, 20)
	addFingerprint(&counts.dequeuedFingerprint, keys, 10)

	for _, value := range []uint64{10, 20} {
		resp, err := session.invoke(0, request{operation: operationEnqueue, value: value})
		if err != nil || resp.status != statusEnqueued {
			t.Fatalf("enqueue %d failed: response=%+v err=%v", value, resp, err)
		}
	}
	resp, err := session.invoke(0, request{operation: operationDequeue})
	if err != nil || resp.status != statusValue || resp.value != 10 {
		t.Fatalf("measured dequeue failed: response=%+v err=%v", resp, err)
	}

	if err := drainAndValidate(session, 0, 4, &counts, keys); err != nil {
		t.Fatal(err)
	}
}

func TestBenchmarkDrainRejectsFabricatedValue(t *testing.T) {
	session, cleanup := startInProcessCandidate(t, 4)
	defer cleanup()
	keys := fingerprintKeys{11, 29}
	counts := benchmarkCounts{enqueued: 1}
	addFingerprint(&counts.enqueuedFingerprint, keys, 10)

	resp, err := session.invoke(0, request{operation: operationEnqueue, value: 99})
	if err != nil || resp.status != statusEnqueued {
		t.Fatalf("enqueue failed: response=%+v err=%v", resp, err)
	}
	if err := drainAndValidate(session, 0, 4, &counts, keys); err == nil {
		t.Fatal("fabricated value passed benchmark multiset validation")
	}
}

func TestBenchmarkDrainRejectsLostValue(t *testing.T) {
	session, cleanup := startInProcessCandidate(t, 4)
	defer cleanup()
	keys := fingerprintKeys{11, 29}
	counts := benchmarkCounts{enqueued: 1}
	addFingerprint(&counts.enqueuedFingerprint, keys, 10)

	if err := drainAndValidate(session, 0, 4, &counts, keys); err == nil {
		t.Fatal("lost value passed benchmark conservation validation")
	}
}
