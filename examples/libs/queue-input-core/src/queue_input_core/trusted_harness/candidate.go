package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sync"
)

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

type candidateSession struct {
	region    *mappedRegion
	command   *exec.Cmd
	done      chan struct{}
	sequences []uint64
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

func startCandidate(config candidateConfig) (*candidateSession, error) {
	region, err := createRegion(config.scenario, config.capacity, config.laneCount)
	if err != nil {
		return nil, err
	}

	var command *exec.Cmd
	if config.useReference {
		executable, err := os.Executable()
		if err != nil {
			_ = region.close()
			return nil, fmt.Errorf("resolve harness executable: %w", err)
		}
		command = exec.Command(executable, "serve-reference", "--shared-memory", region.path)
	} else {
		candidatePath := config.candidate
		if !filepath.IsAbs(candidatePath) {
			candidatePath = filepath.Join(config.workspace, candidatePath)
		}
		stat, err := os.Stat(candidatePath)
		if err != nil {
			_ = region.close()
			return nil, fmt.Errorf("candidate launcher %q: %w", candidatePath, err)
		}
		if stat.IsDir() || stat.Mode()&0o111 == 0 {
			_ = region.close()
			return nil, fmt.Errorf("candidate launcher %q must be an executable file", candidatePath)
		}
		command = exec.Command(candidatePath, "--vibeserve-queue-shm", region.path)
	}
	command.Dir = config.workspace
	log := newBoundedLog(64 * 1024)
	command.Stdout = io.Writer(log)
	command.Stderr = io.Writer(log)
	if err := command.Start(); err != nil {
		_ = region.close()
		return nil, fmt.Errorf("start candidate: %w", err)
	}

	session := &candidateSession{
		region:    region,
		command:   command,
		done:      make(chan struct{}),
		sequences: make([]uint64, config.laneCount),
		log:       log,
	}
	go func() {
		waitErr := command.Wait()
		session.waitMu.Lock()
		session.waitErr = waitErr
		session.waitMu.Unlock()
		close(session.done)
	}()

	for !region.ready() {
		select {
		case <-session.done:
			_ = region.close()
			return nil, session.processError(
				"candidate exited before protocol handshake",
				session.waitError(),
			)
		default:
			runtime.Gosched()
		}
	}
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

func (s *candidateSession) invoke(lane int, req request) (response, error) {
	responses, err := s.invokeBatch(lane, []request{req})
	if err != nil {
		return response{}, err
	}
	return responses[0], nil
}

func (s *candidateSession) invokeBatch(lane int, requests []request) ([]response, error) {
	if lane < 0 || lane >= len(s.sequences) {
		return nil, fmt.Errorf("invalid candidate lane %d", lane)
	}
	if len(requests) == 0 {
		return []response{}, nil
	}
	responses := make([]response, 0, len(requests))
	for batchStart := 0; batchStart < len(requests); batchStart += ringSlots {
		batchEnd := min(batchStart+ringSlots, len(requests))
		firstSequence := s.sequences[lane] + 1
		for _, req := range requests[batchStart:batchEnd] {
			s.sequences[lane]++
			if err := s.region.publish(lane, s.sequences[lane], req); err != nil {
				return nil, err
			}
		}
		lastSequence := s.sequences[lane]
		for sequence := firstSequence; sequence <= lastSequence; sequence++ {
			for {
				if resp, ok := s.region.response(lane, sequence); ok {
					if resp.status == statusError {
						return nil, errors.New("candidate reported a protocol error")
					}
					responses = append(responses, resp)
					s.region.consumeResponse(lane, sequence)
					break
				}
				select {
				case <-s.done:
					return nil, s.processError(
						"candidate exited with an operation in flight",
						s.waitError(),
					)
				default:
					runtime.Gosched()
				}
			}
		}
	}
	return responses, nil
}

func (s *candidateSession) close() error {
	s.closeOnce.Do(func() {
		s.region.stop()
		<-s.done
		waitErr := s.waitError()
		regionErr := s.region.close()
		if waitErr != nil {
			s.closeErr = s.processError("candidate shutdown failed", waitErr)
		}
		if regionErr != nil {
			s.closeErr = errors.Join(s.closeErr, regionErr)
		}
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

func serveReference(sharedMemoryPath string) error {
	region, err := openRegion(sharedMemoryPath)
	if err != nil {
		return err
	}
	defer region.close()

	queue := newReferenceQueue(region.capacity)
	var workers sync.WaitGroup
	workers.Add(region.laneCount)
	for lane := 0; lane < region.laneCount; lane++ {
		go func(lane int) {
			defer workers.Done()
			var previous uint64
			for {
				sequence, req, ok := region.waitForRequest(lane, previous)
				if !ok {
					return
				}
				region.respond(lane, sequence, queue.apply(req))
				previous = sequence
			}
		}(lane)
	}
	region.markReady()
	workers.Wait()
	return nil
}
