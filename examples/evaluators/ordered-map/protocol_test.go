package main

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func TestSpecPrefixOrderAndNeighbors(t *testing.T) {
	spec := &orderedMapSpec{}
	spec.put([]byte{0x00}, []byte("short"))
	spec.put([]byte{0x00, 0x00}, []byte("long"))
	spec.put([]byte{0x01}, []byte("next"))

	min, ok := spec.min()
	if !ok || !bytes.Equal(min.key, []byte{0x00}) {
		t.Fatalf("min = %x", min.key)
	}
	pred, ok := spec.predecessor([]byte{0x01})
	if !ok || !bytes.Equal(pred.key, []byte{0x00, 0x00}) {
		t.Fatalf("predecessor = %x", pred.key)
	}
	items, remaining := spec.rangeQuery([]byte{0x00}, []byte{0x01}, 1)
	if remaining != true || len(items) != 1 || !bytes.Equal(items[0].key, []byte{0x00}) {
		t.Fatalf("range = %+v remaining %v", items, remaining)
	}
}

func TestCorrectnessRequestContainsKeyAndValue(t *testing.T) {
	request := request{operation: operationPut, key: []byte("ab"), value: []byte("xyz")}
	var data bytes.Buffer
	if err := writeRequest(&data, request); err != nil {
		t.Fatal(err)
	}
	if data.Len() != frameSize+5 {
		t.Fatalf("request is %d bytes, want %d", data.Len(), frameSize+5)
	}
	header := data.Bytes()[:frameSize]
	if got := binary.LittleEndian.Uint32(header[:4]); got != uint32(operationPut) {
		t.Fatalf("operation = %d", got)
	}
	if got := binary.LittleEndian.Uint32(header[4:8]); got != 2 {
		t.Fatalf("key length = %d", got)
	}
	if got := binary.LittleEndian.Uint32(header[8:12]); got != 3 {
		t.Fatalf("value length = %d", got)
	}
	if !bytes.Equal(data.Bytes()[frameSize:], []byte("abxyz")) {
		t.Fatal("request payload does not match the trusted key and value")
	}
}

func TestNativeReferenceWorkerRoundTrip(t *testing.T) {
	session, err := startCandidate(candidateConfig{
		workspace:    t.TempDir(),
		useReference: true,
		scenario:     scenarioSWMR,
		maxKeySize:   8,
		maxValueSize: 8,
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

	put := request{operation: operationPut, key: []byte("b"), value: []byte("2")}
	if _, err := session.invoke(0, put); err != nil {
		t.Fatal(err)
	}
	if _, err := session.invoke(0, request{operation: operationPut, key: []byte("a"), value: []byte("1")}); err != nil {
		t.Fatal(err)
	}
	resp, err := session.invoke(0, request{operation: operationMin})
	if err != nil {
		t.Fatal(err)
	}
	if resp.status != statusOK || !bytes.Equal(resp.key, []byte("a")) || !bytes.Equal(resp.value, []byte("1")) {
		t.Fatalf("min = %+v", resp)
	}
	resp, err = session.invoke(0, request{
		operation: operationRange,
		key:       []byte("a"),
		value:     []byte("z"),
		extra:     1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !resp.remaining || len(resp.items) != 1 || !bytes.Equal(resp.items[0].key, []byte("a")) {
		t.Fatalf("range = %+v", resp)
	}
}
