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
	"time"

	"github.com/anishathalye/porcupine"
)

const (
	maxOperationsPerHistory = 32
	historyKeySpace         = 8
)

type mapInput struct {
	Kind  string `json:"kind"`
	Key   string `json:"key"`
	Value []byte `json:"value,omitempty"`
}

type mapOutput struct {
	Ok      bool   `json:"ok,omitempty"`
	Missing bool   `json:"missing,omitempty"`
	Value   []byte `json:"value,omitempty"`
}

type recordedOperation struct {
	ClientID int       `json:"client_id"`
	Input    mapInput  `json:"input"`
	Call     int64     `json:"call"`
	Output   mapOutput `json:"output"`
	Return   int64     `json:"return"`
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

func mapInputFor(req request) mapInput {
	input := mapInput{
		Kind: req.operation.String(),
		Key:  string(req.key),
	}
	if req.operation == operationPut {
		input.Value = append([]byte(nil), req.value...)
	}
	return input
}

func mapOutputFor(req request, resp response) (mapOutput, error) {
	switch req.operation {
	case operationPut:
		if resp.status != statusOK {
			return mapOutput{}, fmt.Errorf("put returned invalid protocol status %d", resp.status)
		}
		if len(resp.value) != 0 {
			return mapOutput{}, errors.New("put returned an unexpected payload")
		}
		return mapOutput{Ok: true}, nil
	case operationGet, operationRemove:
		switch resp.status {
		case statusOK:
			return mapOutput{Ok: true, Value: append([]byte(nil), resp.value...)}, nil
		case statusMissing:
			return mapOutput{Missing: true}, nil
		default:
			return mapOutput{}, fmt.Errorf(
				"%s returned invalid protocol status %d",
				req.operation,
				resp.status,
			)
		}
	default:
		return mapOutput{}, fmt.Errorf("unknown request operation %d", req.operation)
	}
}

type accuracyConfig struct {
	candidateConfig
	operations     int
	trials         int
	seed           int64
	checkBudget    time.Duration
	failureHistory string
}

func clientCountForScenario(s scenario, clients int) (int, error) {
	if clients <= 0 {
		return 0, errors.New("client count must be greater than zero")
	}
	switch s {
	case scenarioSWMR, scenarioMW:
		return clients, nil
	default:
		return 0, fmt.Errorf("unsupported scenario %s", s)
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
	output, err := mapOutputFor(req, resp)
	if err != nil {
		return recordedOperation{}, err
	}
	return recordedOperation{
		ClientID: clientID,
		Input:    mapInputFor(req),
		Call:     call,
		Output:   output,
		Return:   returned,
	}, nil
}

func runBoundaryHistory(config accuracyConfig) ([]recordedOperation, error) {
	sessionConfig := config.candidateConfig
	sessionConfig.laneCount = 1
	sessionConfig.clientCount = 1
	sessionConfig.mixedLane = true
	session, err := startCandidate(sessionConfig)
	if err != nil {
		return nil, err
	}

	var clock atomic.Int64
	history := make([]recordedOperation, 0, 16)
	appendOperation := func(req request) error {
		op, err := recordInvoke(session, 0, 0, req, &clock)
		if err == nil {
			history = append(history, op)
		}
		return err
	}

	key1 := mapPayload(1, config.keySize)
	key2 := mapPayload(2, config.keySize)
	value1 := mapPayload(101, config.valueSize)
	value2 := mapPayload(102, config.valueSize)
	value3 := mapPayload(103, config.valueSize)
	ops := []request{
		{operation: operationGet, key: key1},
		{operation: operationPut, key: key1, value: value1},
		{operation: operationGet, key: key1},
		{operation: operationPut, key: key1, value: value2},
		{operation: operationGet, key: key1},
		{operation: operationRemove, key: key1},
		{operation: operationGet, key: key1},
		{operation: operationRemove, key: key1},
		{operation: operationPut, key: key2, value: value3},
		{operation: operationGet, key: key2},
	}
	for _, req := range ops {
		if err := appendOperation(req); err != nil {
			_ = session.close()
			return history, err
		}
	}
	if err := session.close(); err != nil {
		return history, err
	}
	return history, nil
}

func clientRequest(
	s scenario,
	clientID int,
	trial int,
	opIndex int,
	keySize int,
	valueSize int,
	rng *rand.Rand,
) request {
	key := mapPayload(uint64(rng.Intn(historyKeySpace)), keySize)
	put := request{
		operation: operationPut,
		key:       key,
		value: mapPayload(
			uint64(trial+1)<<48|uint64(clientID)<<32|uint64(opIndex),
			valueSize,
		),
	}
	get := request{operation: operationGet, key: key}
	remove := request{operation: operationRemove, key: key}
	if s == scenarioSWMR && clientID != 0 {
		return get
	}
	if s == scenarioSWMR {
		if rng.Intn(2) == 0 {
			return put
		}
		return remove
	}
	roll := rng.Intn(10)
	switch {
	case roll < 4:
		return put
	case roll < 8:
		return get
	default:
		return remove
	}
}

func runConcurrentHistory(config accuracyConfig, trial int) ([]recordedOperation, error) {
	clientCount, err := clientCountForScenario(config.scenario, config.clientCount)
	if err != nil {
		return nil, err
	}
	sessionConfig := config.candidateConfig
	sessionConfig.laneCount = clientCount
	sessionConfig.clientCount = clientCount
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
				req := clientRequest(
					config.scenario,
					clientID,
					trial,
					opIndex,
					config.keySize,
					config.valueSize,
					rng,
				)
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

// gateFailure turns a non-Ok verdict on the named history into the gate error
// the caller reports.
//
// An undecided verdict is a failure, not a pass: the checker is worst-case
// exponential, so treating porcupine.Unknown as Ok would let a candidate win by
// producing histories the checker cannot decide.
func gateFailure(
	verdict porcupine.CheckResult,
	config accuracyConfig,
	history string,
) error {
	switch verdict {
	case porcupine.Ok:
		return nil
	case porcupine.Illegal:
		return fmt.Errorf("%s violates %s", history, correctnessContract())
	default:
		return fmt.Errorf(
			"%s could not be decided against the %s scenario's %s within the %s check"+
				" budget; the search is worst-case exponential, so raise"+
				" --check-budget or check a smaller workload",
			history,
			config.scenario,
			correctnessContract(),
			config.checkBudget,
		)
	}
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
	// Porcupine reads a zero timeout as unlimited, which is the hang this
	// budget exists to prevent, so the gate requires a positive one.
	if config.checkBudget <= 0 {
		return errors.New("check budget must be greater than zero")
	}
	if _, err := clientCountForScenario(config.scenario, config.clientCount); err != nil {
		return err
	}
	if err := runABIProfiles(config); err != nil {
		return err
	}

	boundary, err := runBoundaryHistory(config)
	if err != nil {
		return fmt.Errorf("boundary history: %w", err)
	}
	if err := gateFailure(
		checkMapHistory(boundary, config.checkBudget),
		config,
		"boundary history",
	); err != nil {
		return errors.Join(err, writeFailureHistory(config.failureHistory, boundary))
	}

	for trial := 0; trial < config.trials; trial++ {
		history, err := runConcurrentHistory(config, trial)
		if err != nil {
			return fmt.Errorf("trial %d (seed %d): %w", trial, config.seed+int64(trial), err)
		}
		if err := gateFailure(
			checkMapHistory(history, config.checkBudget),
			config,
			fmt.Sprintf("trial %d (seed %d)", trial, config.seed+int64(trial)),
		); err != nil {
			return errors.Join(err, writeFailureHistory(config.failureHistory, history))
		}
	}
	return nil
}

func runABIProfiles(config accuracyConfig) error {
	clients, err := clientCountForScenario(config.scenario, config.clientCount)
	if err != nil {
		return err
	}
	profiles := []struct {
		keySize     int
		valueSize   int
		clientCount int
	}{
		{config.keySize, config.valueSize, clients},
		{7, 257, max(2, clients)},
		{8, maxMapValueSize, max(2, clients)},
	}
	seen := make(map[[3]int]bool, len(profiles))
	for _, profile := range profiles {
		key := [3]int{profile.keySize, profile.valueSize, profile.clientCount}
		if seen[key] {
			continue
		}
		seen[key] = true
		probeConfig := config.candidateConfig
		probeConfig.keySize = profile.keySize
		probeConfig.valueSize = profile.valueSize
		probeConfig.clientCount = profile.clientCount
		if err := runCandidateABIProbe(probeConfig); err != nil {
			return fmt.Errorf(
				"ABI profile key_size=%d value_size=%d clients=%d: %w",
				profile.keySize,
				profile.valueSize,
				profile.clientCount,
				err,
			)
		}
	}
	return nil
}
