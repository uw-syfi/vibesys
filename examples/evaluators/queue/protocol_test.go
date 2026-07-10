package main

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func TestQueuePayloadRoundTrip(t *testing.T) {
	for _, size := range []int{8, 64, 1024} {
		value := uint64(3)<<56 | 42
		payload := queuePayload(value, size)
		got, err := queuePayloadValue(payload)
		if err != nil {
			t.Fatal(err)
		}
		if got != value {
			t.Fatalf("size %d decoded value %d, want %d", size, got, value)
		}
	}
}

func TestQueuePayloadRejectsCorruption(t *testing.T) {
	payload := queuePayload(uint64(2)<<56|7, 64)
	payload[31] ^= 1
	if _, err := queuePayloadValue(payload); err == nil {
		t.Fatal("corrupted payload was accepted")
	}
}

func TestCorrectnessRequestContainsCopiedPayload(t *testing.T) {
	const valueSize = 64
	request := request{operation: operationEnqueue, value: 99}
	var data bytes.Buffer
	if err := writeRequest(&data, request, valueSize); err != nil {
		t.Fatal(err)
	}
	if data.Len() != frameSize+valueSize {
		t.Fatalf("request is %d bytes, want %d", data.Len(), frameSize+valueSize)
	}
	header := data.Bytes()[:frameSize]
	if got := binary.LittleEndian.Uint32(header[:4]); got != uint32(operationEnqueue) {
		t.Fatalf("operation = %d", got)
	}
	if got := binary.LittleEndian.Uint32(header[4:8]); got != valueSize {
		t.Fatalf("payload length = %d", got)
	}
	if got := binary.LittleEndian.Uint64(header[8:]); got != 0 {
		t.Fatalf("reserved field = %d", got)
	}
	if !bytes.Equal(data.Bytes()[frameSize:], queuePayload(request.value, valueSize)) {
		t.Fatal("request payload does not match the trusted value")
	}
}

func TestCorrectnessResponseValidatesCopiedPayload(t *testing.T) {
	const valueSize = 64
	payload := queuePayload(123, valueSize)
	var data bytes.Buffer
	var header [frameSize]byte
	binary.LittleEndian.PutUint32(header[:4], uint32(statusValue))
	binary.LittleEndian.PutUint32(header[4:8], valueSize)
	data.Write(header[:])
	data.Write(payload)

	response, err := readResponse(&data, valueSize)
	if err != nil {
		t.Fatal(err)
	}
	if response.status != statusValue || response.value != 123 {
		t.Fatalf("response = %+v", response)
	}
}

func TestNativeReferenceWorkerRoundTrip(t *testing.T) {
	session, err := startCandidate(candidateConfig{
		workspace:     t.TempDir(),
		useReference:  true,
		scenario:      scenarioSPSC,
		capacity:      1,
		valueSize:     64,
		laneCount:     1,
		producerCount: 1,
		consumerCount: 1,
		mixedLane:     true,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := session.close(); err != nil {
			t.Error(err)
		}
	}()

	tests := []struct {
		request request
		status  responseStatus
		value   uint64
	}{
		{request{operation: operationDequeue}, statusEmpty, 0},
		{request{operation: operationEnqueue, value: 41}, statusEnqueued, 0},
		{request{operation: operationEnqueue, value: 42}, statusFull, 0},
		{request{operation: operationDequeue}, statusValue, 41},
	}
	for _, test := range tests {
		response, err := session.invoke(0, test.request)
		if err != nil {
			t.Fatal(err)
		}
		if response.status != test.status || response.value != test.value {
			t.Fatalf(
				"response = status %d value %d, want status %d value %d",
				response.status,
				response.value,
				test.status,
				test.value,
			)
		}
	}
}
