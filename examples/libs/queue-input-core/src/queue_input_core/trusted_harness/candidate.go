package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync"
	"time"
)

const benchmarkPipelineDepth = 64

type boundedLog struct {
	mu        sync.Mutex
	buffer    bytes.Buffer
	remaining int
}

func newBoundedLog(limit int) *boundedLog {
	return &boundedLog{remaining: limit}
}

func (w *boundedLog) Write(value []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	original := len(value)
	if len(value) > w.remaining {
		value = value[:w.remaining]
	}
	_, _ = w.buffer.Write(value)
	w.remaining -= len(value)
	return original, nil
}

func (w *boundedLog) String() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.buffer.String()
}

type candidateLane struct {
	conn net.Conn
	mu   sync.Mutex
}

type candidateSession struct {
	lanes     []candidateLane
	done      chan struct{}
	log       *boundedLog
	waitMu    sync.Mutex
	waitErr   error
	closeOnce sync.Once
	closeErr  error
}

type candidateConfig struct {
	workspace    string
	candidate    string
	useReference bool
	scenario     scenario
	capacity     uint64
	laneCount    int
}

func candidateProtocolArgs(config candidateConfig) []string {
	return []string{
		"--vibeserve-queue-protocol", strconv.FormatUint(uint64(protocolVersion), 10),
		"--vibeserve-queue-fd-base", strconv.Itoa(protocolFDBase),
		"--vibeserve-queue-lanes", strconv.Itoa(config.laneCount),
		"--vibeserve-queue-capacity", strconv.FormatUint(config.capacity, 10),
		"--vibeserve-queue-scenario", config.scenario.String(),
	}
}

func startCandidate(config candidateConfig) (*candidateSession, error) {
	if config.laneCount <= 0 || config.laneCount > maxLaneCount {
		return nil, fmt.Errorf("lane count must be in [1, %d]", maxLaneCount)
	}

	trustedConnections := make([]net.Conn, 0, config.laneCount)
	candidateFiles := make([]*os.File, 0, config.laneCount)
	for lane := 0; lane < config.laneCount; lane++ {
		trusted, candidate, err := createSocketPair(fmt.Sprintf("queue-lane-%d", lane))
		if err != nil {
			_ = closeAll(trustedConnections)
			_ = closeAll(candidateFiles)
			return nil, err
		}
		trustedConnections = append(trustedConnections, trusted)
		candidateFiles = append(candidateFiles, candidate)
	}

	protocolArgs := candidateProtocolArgs(config)
	var command *exec.Cmd
	if config.useReference {
		executable, err := os.Executable()
		if err != nil {
			_ = closeAll(trustedConnections)
			_ = closeAll(candidateFiles)
			return nil, fmt.Errorf("resolve harness executable: %w", err)
		}
		command = exec.Command(executable, append([]string{"serve-reference"}, protocolArgs...)...)
	} else {
		candidatePath := config.candidate
		if !filepath.IsAbs(candidatePath) {
			candidatePath = filepath.Join(config.workspace, candidatePath)
		}
		stat, err := os.Stat(candidatePath)
		if err != nil {
			_ = closeAll(trustedConnections)
			_ = closeAll(candidateFiles)
			return nil, fmt.Errorf("candidate launcher %q: %w", candidatePath, err)
		}
		if stat.IsDir() || stat.Mode()&0o111 == 0 {
			_ = closeAll(trustedConnections)
			_ = closeAll(candidateFiles)
			return nil, fmt.Errorf("candidate launcher %q must be an executable file", candidatePath)
		}
		command = exec.Command(candidatePath, protocolArgs...)
	}
	command.Dir = config.workspace
	command.ExtraFiles = candidateFiles
	log := newBoundedLog(64 * 1024)
	command.Stdout = io.Writer(log)
	command.Stderr = io.Writer(log)
	if err := command.Start(); err != nil {
		_ = closeAll(trustedConnections)
		_ = closeAll(candidateFiles)
		return nil, fmt.Errorf("start candidate: %w", err)
	}
	if err := closeAll(candidateFiles); err != nil {
		_ = closeAll(trustedConnections)
		_ = command.Process.Kill()
		_ = command.Wait()
		return nil, fmt.Errorf("close candidate socket copies: %w", err)
	}

	session := &candidateSession{
		lanes: make([]candidateLane, len(trustedConnections)),
		done:  make(chan struct{}),
		log:   log,
	}
	for lane, conn := range trustedConnections {
		session.lanes[lane].conn = conn
	}
	go func() {
		waitErr := command.Wait()
		session.waitMu.Lock()
		session.waitErr = waitErr
		session.waitMu.Unlock()
		close(session.done)
	}()
	return session, nil
}

func (s *candidateSession) waitError() error {
	s.waitMu.Lock()
	defer s.waitMu.Unlock()
	return s.waitErr
}

func (s *candidateSession) processError(message string, err error) error {
	detail := s.log.String()
	if err == nil && detail == "" {
		return errors.New(message)
	}
	if err == nil {
		return fmt.Errorf("%s\ncandidate output:\n%s", message, detail)
	}
	if detail == "" {
		return fmt.Errorf("%s: %w", message, err)
	}
	return fmt.Errorf("%s: %w\ncandidate output:\n%s", message, err, detail)
}

func (s *candidateSession) communicationError(action string, err error) error {
	select {
	case <-s.done:
		return s.processError("candidate exited while attempting to "+action, s.waitError())
	default:
		return s.processError("candidate failed to "+action, err)
	}
}

func (s *candidateSession) lane(index int) (*candidateLane, error) {
	if index < 0 || index >= len(s.lanes) {
		return nil, fmt.Errorf("invalid candidate lane %d", index)
	}
	return &s.lanes[index], nil
}

func (s *candidateSession) receive(lane *candidateLane) (response, error) {
	resp, err := readResponse(lane.conn)
	if err != nil {
		return response{}, s.communicationError("read a response", err)
	}
	if resp.status == statusError {
		return response{}, errors.New("candidate reported a protocol error")
	}
	return resp, nil
}

func (s *candidateSession) invoke(lane int, req request) (response, error) {
	responses, err := s.invokeBatch(lane, []request{req})
	if err != nil {
		return response{}, err
	}
	return responses[0], nil
}

func (s *candidateSession) invokeBatch(laneIndex int, requests []request) ([]response, error) {
	lane, err := s.lane(laneIndex)
	if err != nil {
		return nil, err
	}
	if len(requests) == 0 {
		return []response{}, nil
	}
	lane.mu.Lock()
	defer lane.mu.Unlock()

	initial := min(benchmarkPipelineDepth, len(requests))
	if err := writeRequests(lane.conn, requests[:initial]); err != nil {
		return nil, s.communicationError("send requests", err)
	}
	sent := initial
	responses := make([]response, 0, len(requests))
	for len(responses) < len(requests) {
		resp, err := s.receive(lane)
		if err != nil {
			return nil, err
		}
		responses = append(responses, resp)
		if sent < len(requests) {
			if err := writeRequest(lane.conn, requests[sent]); err != nil {
				return nil, s.communicationError("send a request", err)
			}
			sent++
		}
	}
	return responses, nil
}

func (s *candidateSession) invokeUntil(
	laneIndex int,
	depth int,
	deadline time.Time,
	nextRequest func() request,
	observe func(request, response) error,
) error {
	if depth <= 0 {
		return errors.New("pipeline depth must be greater than zero")
	}
	lane, err := s.lane(laneIndex)
	if err != nil {
		return err
	}
	lane.mu.Lock()
	defer lane.mu.Unlock()

	if !time.Now().Before(deadline) {
		return nil
	}
	pending := make([]request, depth)
	initial := make([]request, depth)
	for index := range initial {
		initial[index] = nextRequest()
		pending[index] = initial[index]
	}
	if err := writeRequests(lane.conn, initial); err != nil {
		return s.communicationError("fill the request pipeline", err)
	}

	sent := uint64(depth)
	received := uint64(0)
	accepting := true
	for received < sent {
		resp, err := s.receive(lane)
		if err != nil {
			return err
		}
		completedRequest := pending[received%uint64(depth)]
		received++

		if received%uint64(depth) == 0 {
			accepting = time.Now().Before(deadline)
		}
		if accepting {
			next := nextRequest()
			pending[sent%uint64(depth)] = next
			if err := writeRequest(lane.conn, next); err != nil {
				return s.communicationError("refill the request pipeline", err)
			}
			sent++
		}
		if err := observe(completedRequest, resp); err != nil {
			return err
		}
	}
	return nil
}

func (s *candidateSession) close() error {
	s.closeOnce.Do(func() {
		connections := make([]net.Conn, 0, len(s.lanes))
		for index := range s.lanes {
			connections = append(connections, s.lanes[index].conn)
		}
		connectionErr := closeAll(connections)
		<-s.done
		if waitErr := s.waitError(); waitErr != nil {
			s.closeErr = s.processError("candidate shutdown failed", waitErr)
		}
		s.closeErr = errors.Join(s.closeErr, connectionErr)
	})
	return s.closeErr
}

type referenceQueue struct {
	mu       sync.Mutex
	capacity int
	values   []uint64
}

func newReferenceQueue(capacity uint64) *referenceQueue {
	return &referenceQueue{capacity: int(capacity)}
}

func (q *referenceQueue) apply(req request) response {
	q.mu.Lock()
	defer q.mu.Unlock()
	switch req.operation {
	case operationEnqueue:
		if len(q.values) == q.capacity {
			return response{status: statusFull}
		}
		q.values = append(q.values, req.value)
		return response{status: statusEnqueued}
	case operationDequeue:
		if len(q.values) == 0 {
			return response{status: statusEmpty}
		}
		value := q.values[0]
		q.values = q.values[1:]
		return response{status: statusValue, value: value}
	default:
		return response{status: statusError}
	}
}

func serveReferenceLane(conn net.Conn, queue *referenceQueue) error {
	defer conn.Close()
	for {
		req, err := readRequest(conn)
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("read request: %w", err)
		}
		if err := writeResponse(conn, queue.apply(req)); err != nil {
			return fmt.Errorf("write response: %w", err)
		}
	}
}

func serveReferenceConnections(connections []net.Conn, capacity uint64) error {
	queue := newReferenceQueue(capacity)
	errorsByLane := make(chan error, len(connections))
	var workers sync.WaitGroup
	workers.Add(len(connections))
	for lane, conn := range connections {
		go func(lane int, conn net.Conn) {
			defer workers.Done()
			if err := serveReferenceLane(conn, queue); err != nil {
				errorsByLane <- fmt.Errorf("lane %d: %w", lane, err)
			}
		}(lane, conn)
	}
	workers.Wait()
	close(errorsByLane)
	var result error
	for err := range errorsByLane {
		result = errors.Join(result, err)
	}
	return result
}

func serveReference(fdBase, laneCount int, capacity uint64) error {
	connections := make([]net.Conn, 0, laneCount)
	for lane := 0; lane < laneCount; lane++ {
		conn, err := inheritedSocket(fdBase+lane, fmt.Sprintf("queue-lane-%d", lane))
		if err != nil {
			_ = closeAll(connections)
			return err
		}
		connections = append(connections, conn)
	}
	return serveReferenceConnections(connections, capacity)
}
