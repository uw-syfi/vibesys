package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"net"
	"testing"
	"time"
)

func testSocketPair(t *testing.T) (net.Conn, net.Conn) {
	t.Helper()
	trusted, candidateFile, err := createSocketPair("test-lane")
	if err != nil {
		t.Fatal(err)
	}
	candidate, err := net.FileConn(candidateFile)
	closeErr := candidateFile.Close()
	if err != nil {
		_ = trusted.Close()
		t.Fatal(err)
	}
	if closeErr != nil {
		_ = trusted.Close()
		_ = candidate.Close()
		t.Fatal(closeErr)
	}
	return trusted, candidate
}

func startInProcessCandidate(
	t *testing.T,
	capacity uint64,
	laneCount int,
) (*candidateSession, func()) {
	t.Helper()
	trustedConnections := make([]net.Conn, 0, laneCount)
	candidateConnections := make([]net.Conn, 0, laneCount)
	for lane := 0; lane < laneCount; lane++ {
		trusted, candidate := testSocketPair(t)
		trustedConnections = append(trustedConnections, trusted)
		candidateConnections = append(candidateConnections, candidate)
	}
	session := &candidateSession{
		lanes: make([]candidateLane, laneCount),
		done:  make(chan struct{}),
		log:   newBoundedLog(1024),
	}
	for lane, conn := range trustedConnections {
		session.lanes[lane].conn = conn
	}
	go func() {
		err := serveReferenceConnections(candidateConnections, capacity)
		session.waitMu.Lock()
		session.waitErr = err
		session.waitMu.Unlock()
		close(session.done)
	}()
	cleanup := func() {
		if err := session.close(); err != nil {
			t.Error(err)
		}
	}
	return session, cleanup
}

func TestProtocolFramesUseFixedLittleEndianEncoding(t *testing.T) {
	var requestData bytes.Buffer
	req := request{operation: operationEnqueue, value: 0x0102030405060708}
	if err := writeRequest(&requestData, req); err != nil {
		t.Fatal(err)
	}
	if requestData.Len() != frameSize {
		t.Fatalf("request frame is %d bytes, want %d", requestData.Len(), frameSize)
	}
	data := requestData.Bytes()
	if got := binary.LittleEndian.Uint32(data[:4]); got != uint32(operationEnqueue) {
		t.Fatalf("request operation = %d", got)
	}
	if got := binary.LittleEndian.Uint32(data[4:8]); got != 0 {
		t.Fatalf("request reserved field = %d", got)
	}
	if got := binary.LittleEndian.Uint64(data[8:]); got != req.value {
		t.Fatalf("request value = %x, want %x", got, req.value)
	}
	decodedRequest, err := readRequest(bytes.NewReader(data))
	if err != nil || decodedRequest != req {
		t.Fatalf("decoded request = %+v, err = %v", decodedRequest, err)
	}

	var responseData bytes.Buffer
	resp := response{status: statusValue, value: req.value}
	if err := writeResponse(&responseData, resp); err != nil {
		t.Fatal(err)
	}
	decodedResponse, err := readResponse(&responseData)
	if err != nil || decodedResponse != resp {
		t.Fatalf("decoded response = %+v, err = %v", decodedResponse, err)
	}
}

func TestProtocolRejectsMalformedFrames(t *testing.T) {
	tests := map[string]struct {
		data     [frameSize]byte
		response bool
	}{
		"request reserved field": {},
		"request operation":      {},
		"response reserved field": {
			response: true,
		},
		"response status": {
			response: true,
		},
	}
	requestReserved := tests["request reserved field"]
	binary.LittleEndian.PutUint32(requestReserved.data[:], uint32(operationEnqueue))
	binary.LittleEndian.PutUint32(requestReserved.data[4:], 1)
	tests["request reserved field"] = requestReserved

	requestOperation := tests["request operation"]
	binary.LittleEndian.PutUint32(requestOperation.data[:], 99)
	tests["request operation"] = requestOperation

	responseReserved := tests["response reserved field"]
	binary.LittleEndian.PutUint32(responseReserved.data[:], uint32(statusValue))
	binary.LittleEndian.PutUint32(responseReserved.data[4:], 1)
	tests["response reserved field"] = responseReserved

	responseStatus := tests["response status"]
	binary.LittleEndian.PutUint32(responseStatus.data[:], 99)
	tests["response status"] = responseStatus

	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			var err error
			if test.response {
				_, err = readResponse(bytes.NewReader(test.data[:]))
			} else {
				_, err = readRequest(bytes.NewReader(test.data[:]))
			}
			if err == nil {
				t.Fatal("malformed frame was accepted")
			}
		})
	}
}

func TestReferenceServerRoundTripAcrossLanes(t *testing.T) {
	session, cleanup := startInProcessCandidate(t, 1, 2)
	defer cleanup()

	tests := []struct {
		lane    int
		request request
		status  responseStatus
		value   uint64
	}{
		{0, request{operation: operationDequeue}, statusEmpty, 0},
		{0, request{operation: operationEnqueue, value: 41}, statusEnqueued, 0},
		{1, request{operation: operationEnqueue, value: 42}, statusFull, 0},
		{1, request{operation: operationDequeue}, statusValue, 41},
	}
	for _, test := range tests {
		resp, err := session.invoke(test.lane, test.request)
		if err != nil {
			t.Fatal(err)
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
	}
}

func TestOrderedStreamExceedsPipelineWindow(t *testing.T) {
	session, cleanup := startInProcessCandidate(t, benchmarkPipelineDepth*3, 1)
	defer cleanup()

	enqueues := make([]request, benchmarkPipelineDepth*2+2)
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
}

func TestInvokeUntilRefillsBeforeDrainingWindow(t *testing.T) {
	const depth = 4
	trusted, candidate := testSocketPair(t)
	session := &candidateSession{
		lanes: []candidateLane{{conn: trusted}},
		done:  make(chan struct{}),
		log:   newBoundedLog(1024),
	}
	replacementObserved := make(chan struct{})
	go func() {
		defer close(session.done)
		defer candidate.Close()
		setWaitError := func(err error) {
			session.waitMu.Lock()
			session.waitErr = err
			session.waitMu.Unlock()
		}
		for index := 0; index < depth; index++ {
			if _, err := readRequest(candidate); err != nil {
				setWaitError(err)
				return
			}
		}
		time.Sleep(10 * time.Millisecond)
		for index := 0; index < depth; index++ {
			if err := writeResponse(candidate, response{status: statusEnqueued}); err != nil {
				setWaitError(err)
				return
			}
			if index < depth-1 {
				if _, err := readRequest(candidate); err != nil {
					setWaitError(err)
					return
				}
				if index == 0 {
					close(replacementObserved)
				}
			}
		}
		for index := 0; index < depth-1; index++ {
			if err := writeResponse(candidate, response{status: statusEnqueued}); err != nil {
				setWaitError(err)
				return
			}
		}
	}()

	var generated int
	var observed int
	var observedValues []uint64
	err := session.invokeUntil(
		0,
		depth,
		time.Now().Add(2*time.Millisecond),
		func() request {
			generated++
			return request{operation: operationEnqueue, value: uint64(generated)}
		},
		func(req request, resp response) error {
			if resp.status != statusEnqueued {
				return errors.New("unexpected response")
			}
			observed++
			observedValues = append(observedValues, req.value)
			if observed == 1 {
				select {
				case <-replacementObserved:
				case <-time.After(time.Second):
					return errors.New("replacement was not sent before response observation")
				}
			}
			return nil
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-replacementObserved:
	default:
		t.Fatal("pipeline was drained before a replacement request was sent")
	}
	if generated != depth*2-1 {
		t.Fatalf("generated %d requests, want %d", generated, depth*2-1)
	}
	for index, value := range observedValues {
		want := uint64(index + 1)
		if value != want {
			t.Fatalf("observed request %d with value %d, want %d", index, value, want)
		}
	}
	if err := session.close(); err != nil {
		t.Fatal(err)
	}
}
