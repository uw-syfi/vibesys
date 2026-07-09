package main

import (
	"runtime"
	"testing"
)

func TestProtocolLayoutKeepsAtomicFieldsAligned(t *testing.T) {
	atomicOffsets := []int{
		offsetReady,
		offsetStop,
		headerSize + requestPublishedOffset,
		headerSize + requestConsumedOffset,
		headerSize + responsePublishedOffset,
		headerSize + responseConsumedOffset,
	}
	for _, offset := range atomicOffsets {
		if offset%8 != 0 {
			t.Fatalf("atomic field offset %d is not 8-byte aligned", offset)
		}
	}
	if laneSize < responseSlotsOffset+ringSlots*16 {
		t.Fatal("lane does not contain the response payload")
	}
}

func TestReferenceServerRoundTrip(t *testing.T) {
	region, err := createRegion(scenarioMPSC, 1, 2)
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

	tests := []struct {
		lane     int
		sequence uint64
		request  request
		status   responseStatus
		value    uint64
	}{
		{0, 1, request{operation: operationDequeue}, statusEmpty, 0},
		{0, 2, request{operation: operationEnqueue, value: 41}, statusEnqueued, 0},
		{1, 1, request{operation: operationEnqueue, value: 42}, statusFull, 0},
		{1, 2, request{operation: operationDequeue}, statusValue, 41},
	}
	for _, test := range tests {
		if err := region.publish(test.lane, test.sequence, test.request); err != nil {
			t.Fatal(err)
		}
		var resp response
		var ok bool
		for !ok {
			resp, ok = region.response(test.lane, test.sequence)
			runtime.Gosched()
		}
		if resp.status != test.status || resp.value != test.value {
			t.Fatalf(
				"response = status %d value %d, want status %d value %d",
				resp.status,
				resp.value,
				test.status,
				test.value,
			)
		}
		region.consumeResponse(test.lane, test.sequence)
	}

	region.stop()
	if err := <-serverDone; err != nil {
		t.Fatal(err)
	}
	if err := region.close(); err != nil {
		t.Fatal(err)
	}
}

func TestProtocolRingsWrapAcrossPipelinedBatches(t *testing.T) {
	region, err := createRegion(scenarioSPSC, 256, 1)
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

	enqueues := make([]request, ringSlots*2+2)
	for index := range enqueues {
		enqueues[index] = request{operation: operationEnqueue, value: uint64(index + 1)}
	}
	responses, err := session.invokeBatch(0, enqueues)
	if err != nil {
		t.Fatal(err)
	}
	for index, resp := range responses {
		if resp.status != statusEnqueued {
			t.Fatalf("enqueue %d returned status %d", index, resp.status)
		}
	}

	dequeues := make([]request, len(enqueues))
	for index := range dequeues {
		dequeues[index] = request{operation: operationDequeue}
	}
	responses, err = session.invokeBatch(0, dequeues)
	if err != nil {
		t.Fatal(err)
	}
	for index, resp := range responses {
		want := uint64(index + 1)
		if resp.status != statusValue || resp.value != want {
			t.Fatalf(
				"dequeue %d = status %d value %d, want status %d value %d",
				index,
				resp.status,
				resp.value,
				statusValue,
				want,
			)
		}
	}

	region.stop()
	if err := <-serverDone; err != nil {
		t.Fatal(err)
	}
	if err := region.close(); err != nil {
		t.Fatal(err)
	}
}

func TestOpenRegionRejectsInvalidMagic(t *testing.T) {
	region, err := createRegion(scenarioSPSC, 1, 1)
	if err != nil {
		t.Fatal(err)
	}
	region.data[0] = 0
	if _, err := openRegion(region.path); err == nil {
		t.Fatal("invalid protocol magic was accepted")
	}
	if err := region.close(); err != nil {
		t.Fatal(err)
	}
}
