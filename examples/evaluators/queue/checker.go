package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"runtime"
	"sort"
	"sync"
	"sync/atomic"

	"github.com/anishathalye/porcupine"
)

const maxOperationsPerHistory = 32

type queueInput struct {
	Kind  string  `json:"kind"`
	Value *uint64 `json:"value,omitempty"`
}

type queueOutput struct {
	EnqueueOK   *bool   `json:"enqueue_ok,omitempty"`
	DequeueNone bool    `json:"dequeue_none,omitempty"`
	DequeueVal  *uint64 `json:"dequeue_value,omitempty"`
}

type recordedOperation struct {
	ClientID int         `json:"client_id"`
	Input    queueInput  `json:"input"`
	Call     int64       `json:"call"`
	Output   queueOutput `json:"output"`
	Return   int64       `json:"return"`
}

func (op recordedOperation) porcupine() porcupine.Operation {
	return porcupine.Operation{
		ClientId: op.ClientID,
		Input:    op.Input,
		Call:     op.Call,
		Output:   op.Output,
		Return:   op.Return,
	}
}

func queueModel(capacity int) porcupine.Model {
	return porcupine.Model{
		Init: func() any { return []uint64{} },
		Step: func(state, input, output any) (bool, any) {
			current := state.([]uint64)
			in := input.(queueInput)
			out := output.(queueOutput)
			switch in.Kind {
			case "enqueue":
				if in.Value == nil || out.EnqueueOK == nil {
					return false, state
				}
				expected := len(current) < capacity
				if *out.EnqueueOK != expected {
					return false, state
				}
				if !expected {
					return true, state
				}
				next := append([]uint64(nil), current...)
				next = append(next, *in.Value)
				return true, next
			case "dequeue":
				if len(current) == 0 {
					return out.DequeueNone && out.DequeueVal == nil, state
				}
				if out.DequeueNone || out.DequeueVal == nil || *out.DequeueVal != current[0] {
					return false, state
				}
				next := append([]uint64(nil), current[1:]...)
				return true, next
			default:
				return false, state
			}
		},
		Equal: func(first, second any) bool {
			a := first.([]uint64)
			b := second.([]uint64)
			if len(a) != len(b) {
				return false
			}
			for index := range a {
				if a[index] != b[index] {
					return false
				}
			}
			return true
		},
	}
}

func isLinearizable(capacity int, history []recordedOperation) bool {
	operations := make([]porcupine.Operation, 0, len(history))
	for _, op := range history {
		operations = append(operations, op.porcupine())
	}
	return porcupine.CheckOperations(queueModel(capacity), operations)
}

func queueInputFor(req request) queueInput {
	if req.operation == operationEnqueue {
		value := req.value
		return queueInput{Kind: "enqueue", Value: &value}
	}
	return queueInput{Kind: "dequeue"}
}

func queueOutputFor(req request, resp response) (queueOutput, error) {
	switch req.operation {
	case operationEnqueue:
		var value bool
		switch resp.status {
		case statusEnqueued:
			value = true
		case statusFull:
			value = false
		default:
			return queueOutput{}, fmt.Errorf("enqueue returned invalid protocol status %d", resp.status)
		}
		return queueOutput{EnqueueOK: &value}, nil
	case operationDequeue:
		switch resp.status {
		case statusValue:
			value := resp.value
			return queueOutput{DequeueVal: &value}, nil
		case statusEmpty:
			return queueOutput{DequeueNone: true}, nil
		default:
			return queueOutput{}, fmt.Errorf("dequeue returned invalid protocol status %d", resp.status)
		}
	default:
		return queueOutput{}, fmt.Errorf("unknown request operation %d", req.operation)
	}
}

type accuracyConfig struct {
	candidateConfig
	operations     int
	trials         int
	producers      int
	consumers      int
	seed           int64
	failureHistory string
}

func workerCounts(s scenario, producers, consumers int) (int, int, error) {
	if producers <= 0 || consumers <= 0 {
		return 0, 0, errors.New("producer and consumer counts must be greater than zero")
	}
	switch s {
	case scenarioSPSC:
		return 1, 1, nil
	case scenarioMPSC:
		return producers, 1, nil
	case scenarioMPMC:
		return producers, consumers, nil
	default:
		return 0, 0, fmt.Errorf("unsupported scenario %s", s)
	}
}

func recordInvoke(
	session *candidateSession,
	lane int,
	clientID int,
	req request,
	clock *atomic.Int64,
) (recordedOperation, error) {
	call := clock.Add(1)
	resp, err := session.invoke(lane, req)
	returned := clock.Add(1)
	if err != nil {
		return recordedOperation{}, err
	}
	output, err := queueOutputFor(req, resp)
	if err != nil {
		return recordedOperation{}, err
	}
	return recordedOperation{
		ClientID: clientID,
		Input:    queueInputFor(req),
		Call:     call,
		Output:   output,
		Return:   returned,
	}, nil
}

func runBoundaryHistory(config accuracyConfig) ([]recordedOperation, error) {
	sessionConfig := config.candidateConfig
	sessionConfig.laneCount = 1
	sessionConfig.producerCount = 1
	sessionConfig.consumerCount = 1
	sessionConfig.mixedLane = true
	session, err := startCandidate(sessionConfig)
	if err != nil {
		return nil, err
	}

	var clock atomic.Int64
	history := make([]recordedOperation, 0, int(config.capacity)*3+4)
	appendOperation := func(req request) error {
		op, err := recordInvoke(session, 0, 0, req, &clock)
		if err == nil {
			history = append(history, op)
		}
		return err
	}

	if err := appendOperation(request{operation: operationDequeue}); err != nil {
		_ = session.close()
		return history, err
	}
	for value := uint64(0); value < config.capacity; value++ {
		if err := appendOperation(request{operation: operationEnqueue, value: value}); err != nil {
			_ = session.close()
			return history, err
		}
	}
	if err := appendOperation(request{operation: operationEnqueue, value: config.capacity}); err != nil {
		_ = session.close()
		return history, err
	}
	half := config.capacity / 2
	for index := uint64(0); index < half; index++ {
		if err := appendOperation(request{operation: operationDequeue}); err != nil {
			_ = session.close()
			return history, err
		}
	}
	for index := uint64(0); index < half; index++ {
		if err := appendOperation(request{operation: operationEnqueue, value: config.capacity + 1 + index}); err != nil {
			_ = session.close()
			return history, err
		}
	}
	if err := appendOperation(request{operation: operationEnqueue, value: config.capacity*2 + 1}); err != nil {
		_ = session.close()
		return history, err
	}
	for index := uint64(0); index < config.capacity; index++ {
		if err := appendOperation(request{operation: operationDequeue}); err != nil {
			_ = session.close()
			return history, err
		}
	}
	if err := appendOperation(request{operation: operationDequeue}); err != nil {
		_ = session.close()
		return history, err
	}
	if err := session.close(); err != nil {
		return history, err
	}
	return history, nil
}

func runConcurrentHistory(config accuracyConfig, trial int) ([]recordedOperation, error) {
	producers, consumers, err := workerCounts(config.scenario, config.producers, config.consumers)
	if err != nil {
		return nil, err
	}
	clientCount := producers + consumers
	sessionConfig := config.candidateConfig
	sessionConfig.laneCount = clientCount
	sessionConfig.producerCount = producers
	sessionConfig.consumerCount = consumers
	sessionConfig.mixedLane = false
	session, err := startCandidate(sessionConfig)
	if err != nil {
		return nil, err
	}

	operationsPerClient := max(1, config.operations/clientCount)
	perClient := make([][]recordedOperation, clientCount)
	start := make(chan struct{})
	errCh := make(chan error, clientCount)
	var workers sync.WaitGroup
	var clock atomic.Int64
	workers.Add(clientCount)

	for clientID := 0; clientID < clientCount; clientID++ {
		go func(clientID int) {
			defer workers.Done()
			local := make([]recordedOperation, 0, operationsPerClient)
			rng := rand.New(rand.NewSource(config.seed + int64(trial*clientCount+clientID)))
			<-start
			for opIndex := 0; opIndex < operationsPerClient; opIndex++ {
				if rng.Intn(4) == 0 {
					runtime.Gosched()
				}
				req := request{operation: operationDequeue}
				if clientID < producers {
					req = request{
						operation: operationEnqueue,
						value:     uint64(trial+1)<<48 | uint64(clientID)<<32 | uint64(opIndex),
					}
				}
				op, err := recordInvoke(session, clientID, clientID, req, &clock)
				if err != nil {
					errCh <- fmt.Errorf("client %d operation %d: %w", clientID, opIndex, err)
					return
				}
				local = append(local, op)
			}
			perClient[clientID] = local
		}(clientID)
	}
	close(start)
	workers.Wait()
	close(errCh)

	var operationErr error
	for err := range errCh {
		operationErr = errors.Join(operationErr, err)
	}
	closeErr := session.close()
	if operationErr != nil || closeErr != nil {
		return nil, errors.Join(operationErr, closeErr)
	}

	var history []recordedOperation
	for _, clientHistory := range perClient {
		history = append(history, clientHistory...)
	}
	sort.Slice(history, func(first, second int) bool {
		if history[first].Call != history[second].Call {
			return history[first].Call < history[second].Call
		}
		return history[first].ClientID < history[second].ClientID
	})
	return history, nil
}

func writeFailureHistory(path string, history []recordedOperation) error {
	if path == "" {
		return nil
	}
	data, err := json.MarshalIndent(history, "", "  ")
	if err != nil {
		return fmt.Errorf("encode failure history: %w", err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return fmt.Errorf("write failure history %q: %w", path, err)
	}
	return nil
}

func accuracyCapacities(configured uint64) []uint64 {
	capacities := []uint64{configured}
	for _, capacity := range []uint64{1, 2, 3} {
		seen := false
		for _, existing := range capacities {
			if existing == capacity {
				seen = true
				break
			}
		}
		if !seen {
			capacities = append(capacities, capacity)
		}
	}
	return capacities
}

func runAccuracy(config accuracyConfig) error {
	if config.operations <= 0 {
		return errors.New("operations must be greater than zero")
	}
	if config.operations > maxOperationsPerHistory {
		return fmt.Errorf(
			"operations must not exceed %d per history; increase trials for more coverage",
			maxOperationsPerHistory,
		)
	}
	if config.trials <= 0 {
		return errors.New("trials must be greater than zero")
	}
	if _, _, err := workerCounts(config.scenario, config.producers, config.consumers); err != nil {
		return err
	}
	if err := runABIProfiles(config); err != nil {
		return err
	}

	capacities := accuracyCapacities(config.capacity)
	for _, capacity := range capacities {
		capacityConfig := config
		capacityConfig.capacity = capacity
		boundary, err := runBoundaryHistory(capacityConfig)
		if err != nil {
			return fmt.Errorf("boundary history at capacity %d: %w", capacity, err)
		}
		if !isLinearizable(int(capacity), boundary) {
			writeErr := writeFailureHistory(config.failureHistory, boundary)
			return errors.Join(
				fmt.Errorf("boundary history at capacity %d is not linearizable", capacity),
				writeErr,
			)
		}
	}

	for trial := 0; trial < config.trials; trial++ {
		capacity := capacities[trial%len(capacities)]
		trialConfig := config
		trialConfig.capacity = capacity
		history, err := runConcurrentHistory(trialConfig, trial)
		if err != nil {
			return fmt.Errorf(
				"trial %d (seed %d, capacity %d): %w",
				trial,
				config.seed+int64(trial),
				capacity,
				err,
			)
		}
		if !isLinearizable(int(capacity), history) {
			writeErr := writeFailureHistory(config.failureHistory, history)
			return errors.Join(
				fmt.Errorf(
					"trial %d (seed %d, capacity %d) is not linearizable",
					trial,
					config.seed+int64(trial),
					capacity,
				),
				writeErr,
			)
		}
	}
	return nil
}

func runABIProfiles(config accuracyConfig) error {
	producers, consumers, err := workerCounts(
		config.scenario,
		config.producers,
		config.consumers,
	)
	if err != nil {
		return err
	}
	profiles := []struct {
		capacity  uint64
		valueSize int
	}{
		{config.capacity, config.valueSize},
		{7, 257},
		{3, maxQueueValueSize},
	}
	seen := make(map[[2]uint64]bool, len(profiles))
	for _, profile := range profiles {
		key := [2]uint64{profile.capacity, uint64(profile.valueSize)}
		if seen[key] {
			continue
		}
		seen[key] = true
		probeConfig := config.candidateConfig
		probeConfig.capacity = profile.capacity
		probeConfig.valueSize = profile.valueSize
		probeConfig.producerCount = producers
		probeConfig.consumerCount = consumers
		if err := runCandidateABIProbe(probeConfig); err != nil {
			return fmt.Errorf(
				"ABI profile capacity=%d value_size=%d: %w",
				profile.capacity,
				profile.valueSize,
				err,
			)
		}
	}
	return nil
}
