package main

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func TestMapPayloadRoundTrip(t *testing.T) {
	for _, size := range []int{8, 64, 1024} {
		value := uint64(3)<<56 | 42
		payload := mapPayload(value, size)
		got, err := mapPayloadValue(payload)
		if err != nil {
			t.Fatal(err)
		}
		if got != value {
			t.Fatalf("size %d decoded value %d, want %d", size, got, value)
		}
	}
}

func TestMapPayloadRejectsCorruption(t *testing.T) {
	payload := mapPayload(uint64(2)<<56|7, 64)
	payload[31] ^= 1
	if _, err := mapPayloadValue(payload); err == nil {
		t.Fatal("corrupted payload was accepted")
	}
}

func TestCorrectnessRequestContainsCopiedKeyAndValue(t *testing.T) {
	const keySize = 16
	const valueSize = 64
	request := request{
		operation: operationPut,
		key:       mapPayload(7, keySize),
		value:     mapPayload(99, valueSize),
	}
	var data bytes.Buffer
	if err := writeRequest(&data, request, keySize, valueSize); err != nil {
		t.Fatal(err)
	}
	if data.Len() != frameSize+keySize+valueSize {
		t.Fatalf("request is %d bytes, want %d", data.Len(), frameSize+keySize+valueSize)
	}
	header := data.Bytes()[:frameSize]
	if got := binary.LittleEndian.Uint32(header[:4]); got != uint32(operationPut) {
		t.Fatalf("operation = %d", got)
	}
	if got := binary.LittleEndian.Uint32(header[4:8]); got != keySize {
		t.Fatalf("key length = %d", got)
	}
	if got := binary.LittleEndian.Uint32(header[8:12]); got != valueSize {
		t.Fatalf("value length = %d", got)
	}
	if got := binary.LittleEndian.Uint32(header[12:]); got != 0 {
		t.Fatalf("reserved = %d", got)
	}
	body := data.Bytes()[frameSize:]
	if !bytes.Equal(body[:keySize], request.key) || !bytes.Equal(body[keySize:], request.value) {
		t.Fatal("request payload does not match the trusted key and value")
	}
}

func TestCorrectnessResponseValidatesCopiedPayload(t *testing.T) {
	const valueSize = 64
	payload := mapPayload(123, valueSize)
	var data bytes.Buffer
	var header [frameSize]byte
	binary.LittleEndian.PutUint32(header[:4], uint32(statusOK))
	binary.LittleEndian.PutUint32(header[4:8], valueSize)
	data.Write(header[:])
	data.Write(payload)

	response, err := readResponse(&data, valueSize)
	if err != nil {
		t.Fatal(err)
	}
	if response.status != statusOK || !bytes.Equal(response.value, payload) {
		t.Fatalf("response = %+v", response)
	}
}

func TestNativeReferenceWorkerRoundTrip(t *testing.T) {
	session, err := startCandidate(candidateConfig{
		workspace:    t.TempDir(),
		useReference: true,
		scenario:     scenarioSWMR,
		keySize:      64,
		valueSize:    64,
		clientCount:  1,
		laneCount:    1,
		mixedLane:    true,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := session.close(); err != nil {
			t.Error(err)
		}
	}()

	key1 := mapPayload(1, 64)
	key2 := mapPayload(2, 64)
	value1 := mapPayload(11, 64)
	value2 := mapPayload(12, 64)
	tests := []struct {
		request request
		status  responseStatus
		value   []byte
	}{
		{request{operation: operationGet, key: key1}, statusMissing, nil},
		{request{operation: operationPut, key: key1, value: value1}, statusOK, nil},
		{request{operation: operationGet, key: key1}, statusOK, value1},
		{request{operation: operationPut, key: key1, value: value2}, statusOK, nil},
		{request{operation: operationGet, key: key1}, statusOK, value2},
		{request{operation: operationRemove, key: key1}, statusOK, value2},
		{request{operation: operationGet, key: key1}, statusMissing, nil},
		{request{operation: operationRemove, key: key1}, statusMissing, nil},
		{request{operation: operationPut, key: key2, value: value1}, statusOK, nil},
		{request{operation: operationGet, key: key2}, statusOK, value1},
	}
	for _, test := range tests {
		response, err := session.invoke(0, test.request)
		if err != nil {
			t.Fatal(err)
		}
		if response.status != test.status || !bytes.Equal(response.value, test.value) {
			t.Fatalf(
				"response = status %s value %v, want status %s value %v",
				response.status,
				response.value,
				test.status,
				test.value,
			)
		}
	}
}
