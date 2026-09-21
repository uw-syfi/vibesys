package main

import (
	"bytes"
	"errors"
	"fmt"
	"math/rand"
	"runtime"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/anishathalye/porcupine"
)

const (
	maxOperationsPerHistory = 32
	concurrentKeyCount      = 8
)

type mapInput struct {
	Kind     string `json:"kind"`
	Key      []byte `json:"key,omitempty"`
	Value    []byte `json:"value,omitempty"`
	MaxItems uint32 `json:"max_items,omitempty"`
	Weak     bool   `json:"weak,omitempty"`
}

type rangePair struct {
	Key   []byte `json:"key"`
	Value []byte `json:"value"`
}

type mapOutput struct {
	Status    responseStatus `json:"status"`
	Key       []byte         `json:"key,omitempty"`
	Value     []byte         `json:"value,omitempty"`
	Items     []rangePair    `json:"items,omitempty"`
	Remaining bool           `json:"remaining"`
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
	in := mapInput{
		Key:   cloneBytes(req.key),
		Value: cloneBytes(req.value),
	}
	switch req.operation {
	case operationPut:
		in.Kind = "put"
	case operationGet:
		in.Kind = "get"
	case operationRemove:
		in.Kind = "remove"
	case operationMin:
		in.Kind = "min"
	case operationMax:
		in.Kind = "max"
	case operationPredecessor:
		in.Kind = "predecessor"
	case operationSuccessor:
		in.Kind = "successor"
	case operationRange:
		in.Kind = "range"
		in.MaxItems = req.extra
	}
	return in
}

func mapOutputFor(req request, resp response) (mapOutput, error) {
	switch resp.status {
	case statusOK, statusMissing:
	default:
		return mapOutput{}, fmt.Errorf("invalid protocol status %d", resp.status)
	}
	if req.operation == operationPut && resp.status != statusOK {
		return mapOutput{}, fmt.Errorf("put returned invalid protocol status %d", resp.status)
	}
	if req.operation == operationRange && resp.status != statusOK {
		return mapOutput{}, fmt.Errorf("range returned invalid protocol status %d", resp.status)
	}
	out := mapOutput{
		Status:    resp.status,
		Key:       cloneBytes(resp.key),
		Value:     cloneBytes(resp.value),
		Remaining: resp.remaining,
	}
	if req.operation == operationRange {
		out.Items = make([]rangePair, len(resp.items))
		for index, item := range resp.items {
			out.Items[index] = rangePair{
				Key:   cloneBytes(item.key),
				Value: cloneBytes(item.value),
			}
		}
	}
	return out, nil
}

type accuracyConfig struct {
	candidateConfig
	operations  int
	trials      int
	clients     int
	seed        int64
	checkBudget time.Duration
}

func clientCount(s scenario, clients int) (int, error) {
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
	ops := boundaryOps(config.maxKeySize, config.maxValueSize)
	history := make([]recordedOperation, 0, len(ops))
	for index, req := range ops {
		op, err := recordInvoke(session, 0, 0, req, &clock)
		if err != nil {
			_ = session.close()
			return history, fmt.Errorf("boundary operation %d: %w", index, err)
		}
		history = append(history, op)
	}
	if err := session.close(); err != nil {
		return history, err
	}
	return history, nil
}

func runConcurrentHistory(config accuracyConfig, trial int) ([]recordedOperation, error) {
	clients, err := clientCount(config.scenario, config.clients)
	if err != nil {
		return nil, err
	}
	sessionConfig := config.candidateConfig
	sessionConfig.laneCount = clients
	sessionConfig.clientCount = clients
	sessionConfig.mixedLane = false
	session, err := startCandidate(sessionConfig)
	if err != nil {
		return nil, err
	}

	operationsPerClient := max(1, config.operations/clients)
	keys := concurrentKeys(config.maxKeySize)
	perClient := make([][]recordedOperation, clients)
	start := make(chan struct{})
	errCh := make(chan error, clients)
	var workers sync.WaitGroup
	var clock atomic.Int64
	workers.Add(clients)

	for clientID := 0; clientID < clients; clientID++ {
		go func(clientID int) {
			defer workers.Done()
			local := make([]recordedOperation, 0, operationsPerClient)
			rng := rand.New(rand.NewSource(config.seed + int64(trial*clients+clientID)))
			<-start
			for opIndex := 0; opIndex < operationsPerClient; opIndex++ {
				if rng.Intn(4) == 0 {
					runtime.Gosched()
				}
				req := concurrentOp(
					rng,
					config.scenario,
					clientID,
					clients,
					config.maxKeySize,
					config.maxValueSize,
					keys,
					trial,
					opIndex,
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
	annotateWeakOrderedOps(history)
	return history, nil
}

func isOrderedKind(kind string) bool {
	switch kind {
	case "min", "max", "predecessor", "successor", "range":
		return true
	default:
		return false
	}
}

func isMutationKind(kind string) bool {
	return kind == "put" || kind == "remove"
}

func intervalsOverlap(first, second recordedOperation) bool {
	return first.Call < second.Return && second.Call < first.Return
}

// annotateWeakOrderedOps marks min/max/predecessor/successor/range operations
// that overlap a put or remove. Those walks are checked as weakly consistent
// skip-list iteration rather than as linearizable snapshots. Non-overlapping
// ordered ops keep the sequential spec, including the ABI probe and boundary
// history.
func annotateWeakOrderedOps(history []recordedOperation) {
	for index := range history {
		if !isOrderedKind(history[index].Input.Kind) {
			continue
		}
		for other := range history {
			if other == index || !isMutationKind(history[other].Input.Kind) {
				continue
			}
			if intervalsOverlap(history[index], history[other]) {
				history[index].Input.Weak = true
				break
			}
		}
	}
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
	if _, err := clientCount(config.scenario, config.clients); err != nil {
		return err
	}
	if err := runABIProfiles(config); err != nil {
		return err
	}

	boundary, err := runBoundaryHistory(config)
	if err != nil {
		return fmt.Errorf("ordered boundary history: %w", err)
	}
	if err := gateFailure(
		checkOrderedMapHistory(boundary, config.checkBudget),
		config,
		"ordered boundary history",
	); err != nil {
		return err
	}

	for trial := 0; trial < config.trials; trial++ {
		history, err := runConcurrentHistory(config, trial)
		if err != nil {
			return fmt.Errorf("trial %d (seed %d): %w", trial, config.seed+int64(trial), err)
		}
		if err := gateFailure(
			checkOrderedMapHistory(history, config.checkBudget),
			config,
			fmt.Sprintf("trial %d (seed %d)", trial, config.seed+int64(trial)),
		); err != nil {
			return err
		}
	}
	return nil
}

func runABIProfiles(config accuracyConfig) error {
	clients, err := clientCount(config.scenario, config.clients)
	if err != nil {
		return err
	}
	profiles := []struct {
		maxKeySize   int
		maxValueSize int
	}{
		{config.maxKeySize, config.maxValueSize},
		{8, 257},
		{3, 64},
	}
	seen := make(map[[2]int]bool, len(profiles))
	for _, profile := range profiles {
		key := [2]int{profile.maxKeySize, profile.maxValueSize}
		if seen[key] {
			continue
		}
		seen[key] = true
		probeConfig := config.candidateConfig
		probeConfig.maxKeySize = profile.maxKeySize
		probeConfig.maxValueSize = profile.maxValueSize
		probeConfig.clientCount = clients
		if err := runCandidateABIProbe(probeConfig); err != nil {
			return fmt.Errorf(
				"ABI profile max_key_size=%d max_value_size=%d: %w",
				profile.maxKeySize,
				profile.maxValueSize,
				err,
			)
		}
	}
	return nil
}

func concurrentOp(
	rng *rand.Rand,
	s scenario,
	clientID int,
	clients int,
	maxKeySize int,
	maxValueSize int,
	keys [][]byte,
	trial int,
	opIndex int,
) request {
	if s == scenarioSWMR && clients > 1 && clientID == 0 {
		return writerOp(rng, maxValueSize, keys, trial, clientID, opIndex)
	}
	if s == scenarioSWMR && clients > 1 {
		return readerOp(rng, maxKeySize, keys)
	}
	return mixedOp(rng, maxKeySize, maxValueSize, keys, trial, clientID, opIndex)
}

func writerOp(
	rng *rand.Rand,
	maxValueSize int,
	keys [][]byte,
	trial int,
	clientID int,
	opIndex int,
) request {
	if rng.Intn(10) < 7 {
		return request{
			operation: operationPut,
			key:       pickKey(rng, keys),
			value:     opValue(trial, clientID, opIndex, maxValueSize),
		}
	}
	return request{operation: operationRemove, key: pickKey(rng, keys)}
}

func readerOp(rng *rand.Rand, maxKeySize int, keys [][]byte) request {
	choice := rng.Intn(100)
	switch {
	case choice < 40:
		return request{operation: operationGet, key: pickKey(rng, keys)}
	case choice < 52:
		return request{operation: operationMin}
	case choice < 64:
		return request{operation: operationMax}
	case choice < 76:
		return request{operation: operationPredecessor, key: pickKey(rng, keys)}
	case choice < 88:
		return request{operation: operationSuccessor, key: pickKey(rng, keys)}
	default:
		return rangeOp(rng, maxKeySize, keys)
	}
}

func mixedOp(
	rng *rand.Rand,
	maxKeySize int,
	maxValueSize int,
	keys [][]byte,
	trial int,
	clientID int,
	opIndex int,
) request {
	choice := rng.Intn(100)
	switch {
	case choice < 22:
		return request{
			operation: operationPut,
			key:       pickKey(rng, keys),
			value:     opValue(trial, clientID, opIndex, maxValueSize),
		}
	case choice < 40:
		return request{operation: operationGet, key: pickKey(rng, keys)}
	case choice < 54:
		return request{operation: operationRemove, key: pickKey(rng, keys)}
	case choice < 62:
		return request{operation: operationMin}
	case choice < 70:
		return request{operation: operationMax}
	case choice < 79:
		return request{operation: operationPredecessor, key: pickKey(rng, keys)}
	case choice < 88:
		return request{operation: operationSuccessor, key: pickKey(rng, keys)}
	default:
		return rangeOp(rng, maxKeySize, keys)
	}
}

func rangeOp(rng *rand.Rand, maxKeySize int, keys [][]byte) request {
	start, end := rangeBounds(rng, keys, maxKeySize)
	return request{
		operation: operationRange,
		key:       start,
		value:     end,
		extra:     uint32(1 + rng.Intn(4)),
	}
}

func concurrentKeys(maxKeySize int) [][]byte {
	keys := make([][]byte, concurrentKeyCount)
	for index := 0; index < concurrentKeyCount; index++ {
		length := min(maxKeySize, 2)
		key := make([]byte, length)
		if length > 0 {
			key[length-1] = byte(index)
		}
		keys[index] = key
	}
	return keys
}

func pickKey(rng *rand.Rand, keys [][]byte) []byte {
	return cloneBytes(keys[rng.Intn(len(keys))])
}

func rangeBounds(rng *rand.Rand, keys [][]byte, maxKeySize int) ([]byte, []byte) {
	start := pickKey(rng, keys)
	end := pickKey(rng, keys)
	if bytes.Compare(start, end) < 0 {
		return start, end
	}
	high := randomHighKey(maxKeySize)
	if bytes.Compare(start, high) < 0 {
		return start, high
	}
	low := cloneBytes(keys[0])
	if bytes.Compare(low, start) < 0 {
		return low, start
	}
	return []byte{}, start
}

func opValue(trial, clientID, opIndex, maxValueSize int) []byte {
	length := min(4, maxValueSize)
	out := make([]byte, length)
	n := uint32(trial+1)<<16 | uint32(clientID)<<8 | uint32(opIndex)
	for index := range out {
		shift := 8 * (len(out) - 1 - index)
		out[index] = byte(n >> shift)
	}
	return out
}

func boundaryOps(maxKeySize, maxValueSize int) []request {
	key := func(values ...byte) []byte {
		if len(values) > maxKeySize {
			values = values[:maxKeySize]
		}
		return append([]byte(nil), values...)
	}
	value := func(tag byte) []byte {
		length := min(3, maxValueSize)
		out := make([]byte, length)
		for index := range out {
			out[index] = tag + byte(index)
		}
		return out
	}
	a, b, c := key(0x01), key(0x02), key(0x03)
	if maxKeySize >= 2 {
		a, b, c = key(0x00), key(0x00, 0x00), key(0x01)
	}
	return []request{
		{operation: operationMin},
		{operation: operationMax},
		{operation: operationRange, key: a, value: c, extra: 4},
		{operation: operationPredecessor, key: a},
		{operation: operationSuccessor, key: a},
		{operation: operationPut, key: c, value: value(3)},
		{operation: operationPut, key: a, value: value(1)},
		{operation: operationPut, key: b, value: value(2)},
		{operation: operationMin},
		{operation: operationMax},
		{operation: operationPredecessor, key: c},
		{operation: operationSuccessor, key: a},
		{operation: operationRange, key: a, value: key(0xff), extra: 2},
		{operation: operationGet, key: b},
		{operation: operationPut, key: b, value: value(9)},
		{operation: operationRemove, key: a},
		{operation: operationGet, key: a},
		{operation: operationMin},
	}
}

func randomHighKey(maxKeySize int) []byte {
	out := make([]byte, maxKeySize)
	for index := range out {
		out[index] = 0xff
	}
	return out
}

func cloneBytes(value []byte) []byte {
	if value == nil {
		return nil
	}
	return append([]byte(nil), value...)
}
