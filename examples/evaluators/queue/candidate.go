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

type candidateLane struct {
	conn net.Conn
	mu   sync.Mutex
}

type candidateSession struct {
	lanes     []candidateLane
	valueSize int
	done      chan struct{}
	log       *boundedLog
	waitMu    sync.Mutex
	waitErr   error
	closeOnce sync.Once
	closeErr  error
}

type candidateConfig struct {
	workspace     string
	candidate     string
	useReference  bool
	scenario      scenario
	capacity      uint64
	valueSize     int
	laneCount     int
	producerCount int
	consumerCount int
	mixedLane     bool
}

func candidateLibraryPath(config candidateConfig) (string, error) {
	if config.useReference {
		return "", nil
	}
	path := config.candidate
	if !filepath.IsAbs(path) {
		path = filepath.Join(config.workspace, path)
	}
	stat, err := os.Stat(path)
	if err != nil {
		return "", fmt.Errorf("candidate library %q: %w", path, err)
	}
	if stat.IsDir() || !stat.Mode().IsRegular() {
		return "", fmt.Errorf("candidate library %q must be a regular file", path)
	}
	return path, nil
}

func candidateSourceArgs(config candidateConfig) ([]string, error) {
	if config.useReference {
		return []string{"--reference"}, nil
	}
	path, err := candidateLibraryPath(config)
	if err != nil {
		return nil, err
	}
	return []string{"--library", path}, nil
}

func runCandidateABIProbe(config candidateConfig) error {
	runner, err := nativeRunnerPath()
	if err != nil {
		return err
	}
	sourceArgs, err := candidateSourceArgs(config)
	if err != nil {
		return err
	}
	args := append([]string{"probe"}, sourceArgs...)
	args = append(args,
		"--capacity", strconv.FormatUint(config.capacity, 10),
		"--value-size", strconv.Itoa(config.valueSize),
		"--producers", strconv.Itoa(config.producerCount),
		"--consumers", strconv.Itoa(config.consumerCount),
	)
	command := exec.Command(runner, args...)
	command.Dir = config.workspace
	log := newBoundedLog(64 * 1024)
	command.Stdout = io.Writer(log)
	command.Stderr = io.Writer(log)
	if err := command.Run(); err != nil {
		return fmt.Errorf(
			"native ABI probe failed: %w\nnative runner output:\n%s",
			err,
			log.String(),
		)
	}
	return nil
}

func startCandidate(config candidateConfig) (*candidateSession, error) {
	if config.laneCount <= 0 || config.laneCount > maxLaneCount {
		return nil, fmt.Errorf("lane count must be in [1, %d]", maxLaneCount)
	}
	if config.producerCount <= 0 || config.consumerCount <= 0 {
		return nil, errors.New("candidate session requires producers and consumers")
	}
	if config.mixedLane && config.laneCount != 1 {
		return nil, errors.New("mixed candidate session requires exactly one lane")
	}
	if !config.mixedLane && config.laneCount != config.producerCount+config.consumerCount {
		return nil, errors.New("candidate lane count does not match worker counts")
	}

	trustedConnections := make([]net.Conn, 0, config.laneCount)
	runnerFiles := make([]*os.File, 0, config.laneCount)
	for lane := 0; lane < config.laneCount; lane++ {
		trusted, runner, err := createSocketPair(fmt.Sprintf("queue-lane-%d", lane))
		if err != nil {
			_ = closeAll(trustedConnections)
			_ = closeAll(runnerFiles)
			return nil, err
		}
		trustedConnections = append(trustedConnections, trusted)
		runnerFiles = append(runnerFiles, runner)
	}

	runner, err := nativeRunnerPath()
	if err != nil {
		_ = closeAll(trustedConnections)
		_ = closeAll(runnerFiles)
		return nil, err
	}
	sourceArgs, err := candidateSourceArgs(config)
	if err != nil {
		_ = closeAll(trustedConnections)
		_ = closeAll(runnerFiles)
		return nil, err
	}
	args := append([]string{"worker"}, sourceArgs...)
	args = append(args,
		"--fd-base", strconv.Itoa(protocolFDBase),
		"--lanes", strconv.Itoa(config.laneCount),
		"--producers", strconv.Itoa(config.producerCount),
		"--consumers", strconv.Itoa(config.consumerCount),
		"--capacity", strconv.FormatUint(config.capacity, 10),
		"--value-size", strconv.Itoa(config.valueSize),
	)
	if config.mixedLane {
		args = append(args, "--mixed-lane")
	}
	command := exec.Command(runner, args...)
	command.Dir = config.workspace
	command.ExtraFiles = runnerFiles
	log := newBoundedLog(64 * 1024)
	command.Stdout = io.Writer(log)
	command.Stderr = io.Writer(log)
	if err := command.Start(); err != nil {
		_ = closeAll(trustedConnections)
		_ = closeAll(runnerFiles)
		return nil, fmt.Errorf("start trusted native worker: %w", err)
	}
	if err := closeAll(runnerFiles); err != nil {
		_ = closeAll(trustedConnections)
		_ = command.Process.Kill()
		_ = command.Wait()
		return nil, fmt.Errorf("close native worker socket copies: %w", err)
	}

	session := &candidateSession{
		lanes:     make([]candidateLane, len(trustedConnections)),
		valueSize: config.valueSize,
		done:      make(chan struct{}),
		log:       log,
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
		return fmt.Errorf("%s\nnative runner output:\n%s", message, detail)
	}
	if detail == "" {
		return fmt.Errorf("%s: %w", message, err)
	}
	return fmt.Errorf("%s: %w\nnative runner output:\n%s", message, err, detail)
}

func (s *candidateSession) communicationError(action string, err error) error {
	select {
	case <-s.done:
		return s.processError("native worker exited while attempting to "+action, s.waitError())
	default:
		return s.processError("native worker failed to "+action, err)
	}
}

func (s *candidateSession) invoke(laneIndex int, req request) (response, error) {
	if laneIndex < 0 || laneIndex >= len(s.lanes) {
		return response{}, fmt.Errorf("invalid candidate lane %d", laneIndex)
	}
	lane := &s.lanes[laneIndex]
	lane.mu.Lock()
	defer lane.mu.Unlock()
	if err := writeRequest(lane.conn, req, s.valueSize); err != nil {
		return response{}, s.communicationError("send a correctness request", err)
	}
	resp, err := readResponse(lane.conn, s.valueSize)
	if err != nil {
		return response{}, s.communicationError("read a correctness response", err)
	}
	if resp.status == statusError {
		return response{}, errors.New("candidate reported an ABI or worker error")
	}
	return resp, nil
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
			s.closeErr = s.processError("native worker shutdown failed", waitErr)
		}
		s.closeErr = errors.Join(s.closeErr, connectionErr)
	})
	return s.closeErr
}
