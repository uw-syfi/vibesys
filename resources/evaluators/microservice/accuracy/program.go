package accuracy

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"sync"
	"sync/atomic"
)

const (
	// ProgramSchemaVersion is the schema version accepted by DecodeProgram and
	// VerifyProgram.
	ProgramSchemaVersion = 1
	// TraceSchemaVersion is the schema version emitted in candidate traces.
	TraceSchemaVersion = 1
	// ProgramCounterexampleSchemaVersion is the schema version emitted for a
	// failed program verification.
	ProgramCounterexampleSchemaVersion = 1
	// MaxParallelCalls bounds exact legal-serialization search. Wider
	// concurrency histories should be divided into multiple quiescent groups or
	// checked by an application-specific property.
	MaxParallelCalls = 8
)

// EventKind identifies the one event represented by a program or trace step.
type EventKind string

const (
	EventCall     EventKind = "call"
	EventParallel EventKind = "parallel"
	EventCrash    EventKind = "crash"
	EventStart    EventKind = "start"
)

// Program is a replayable sequence of candidate events. It contains stimuli,
// never expected observations. Applications generate programs independently
// from the Reference used to decide whether an execution is correct.
//
// Programs and their actions must round-trip through encoding/json. VerifyProgram
// takes a JSON snapshot before executing anything so caller-owned actions cannot
// be mutated through the candidate or reference boundary.
type Program[A any] struct {
	SchemaVersion int       `json:"schema_version"`
	ID            string    `json:"id"`
	Steps         []Step[A] `json:"steps"`
}

// Step is a closed union of program events. Exactly one field must be non-nil.
// Call and Parallel require a running candidate. Crash requires a running
// candidate, and Start requires a candidate stopped by the preceding lifecycle
// history. Crash and Start therefore occur only at quiescent step boundaries.
type Step[A any] struct {
	Call     *Call[A]     `json:"call,omitempty"`
	Parallel *Parallel[A] `json:"parallel,omitempty"`
	Crash    *Crash       `json:"crash,omitempty"`
	Start    *Start       `json:"start,omitempty"`
}

// Call gives one application action a stable identity in a program and trace.
// Call IDs must be nonempty and unique across the whole program.
type Call[A any] struct {
	ID     string `json:"id"`
	Action A      `json:"action"`
}

// Parallel is a group of calls launched concurrently. All calls finish before
// the next step. Verification accepts the observations when at least one
// sequential ordering admitted by the Reference produces them and respects
// the candidate trace's invocation/completion precedence.
type Parallel[A any] struct {
	Calls []Call[A] `json:"calls"`
}

// Crash marks a candidate crash. It has no fields in schema version 1.
type Crash struct{}

// Start marks a candidate start after a crash. It has no fields in schema
// version 1.
type Start struct{}

// Reference defines the canonical application behavior used to judge a
// candidate. S is logical application state, A is an application action, and O
// is its externally observable result.
//
// Step must implement the application's sequential semantics and should be
// total for every generated action. Application-level failures, such as a
// rejected reservation, belong in O; a returned error means the reference
// itself could not evaluate the action and aborts verification. AfterCrash
// defines which acknowledged logical state survives a crash. Start does not
// change logical state, so it has no corresponding reference method.
//
// States must round-trip through encoding/json. The verifier clones a state
// before each transition, which isolates legal-serialization branches without
// requiring Clone to be part of the application-facing interface.
type Reference[S, A, O any] interface {
	Initial() (S, error)
	Step(S, A) (next S, expected O, err error)
	AfterCrash(S) (S, error)
	Equal(expected, actual O) bool
}

// Candidate binds abstract application actions to the system under test.
// Invoke should encode one action as a real request and decode its observation;
// it may be nil when a program contains only lifecycle events. VerifyProgram
// calls Invoke concurrently for every call in a Parallel step, so the adapter
// and the runtime it uses must support concurrent invocation.
// Crash must return after the candidate is stopped; Start must return after it
// is ready for subsequent calls. A lifecycle hook may be nil when the program
// contains no corresponding event. VerifyProgram never overlaps lifecycle
// hooks with Invoke calls.
type Candidate[A, O any] struct {
	Invoke Execute[A, O]
	Crash  func(context.Context) error
	Start  func(context.Context) error
}

// CallTrace records one candidate observation. Error is reserved for adapter
// or transport failures. Expected application-level failures should be encoded
// in Observation so the Reference can compare them. InvokedOrder and
// CompletedOrder are monotonically increasing logical event positions within
// one program step; they constrain the legal serializations of parallel calls.
type CallTrace[O any] struct {
	ID             string `json:"id"`
	InvokedOrder   int64  `json:"invoked_order"`
	CompletedOrder int64  `json:"completed_order"`
	Observation    *O     `json:"observation,omitempty"`
	Error          string `json:"error,omitempty"`
}

// TraceStep records the candidate evidence produced by one program step.
// Calls remain in declaration order even when Kind is EventParallel. The order
// fields record logical client events, not wall-clock timestamps.
type TraceStep[O any] struct {
	Step  int            `json:"step"`
	Kind  EventKind      `json:"kind"`
	Calls []CallTrace[O] `json:"calls,omitempty"`
	Error string         `json:"error,omitempty"`
}

// Trace is stable, versioned candidate evidence for a Program execution.
type Trace[O any] struct {
	SchemaVersion int            `json:"schema_version"`
	ProgramID     string         `json:"program_id"`
	Steps         []TraceStep[O] `json:"steps"`
}

// ProgramCounterexample contains the shortest executed program prefix for
// which the candidate has no matching reference execution. Trace contains the
// candidate evidence for that prefix. Reason is diagnostic text, not a stable
// machine-readable classification.
type ProgramCounterexample[A, O any] struct {
	SchemaVersion int        `json:"schema_version"`
	Program       Program[A] `json:"program"`
	Trace         Trace[O]   `json:"trace"`
	Step          int        `json:"step"`
	Reason        string     `json:"reason"`
}

func (c *ProgramCounterexample[A, O]) Error() string {
	encoded, err := json.Marshal(c)
	if err != nil {
		return fmt.Sprintf("accuracy program %q mismatch at step %d", c.Program.ID, c.Step)
	}
	return "accuracy program mismatch: " + string(encoded)
}

// DecodeProgram strictly decodes one Program. It rejects unknown fields,
// trailing JSON, invalid event unions, duplicate call IDs, and invalid
// candidate lifecycle order so persisted inputs cannot be reinterpreted during
// replay.
func DecodeProgram[A any](input io.Reader) (Program[A], error) {
	decoder := json.NewDecoder(input)
	decoder.DisallowUnknownFields()
	var program Program[A]
	if err := decoder.Decode(&program); err != nil {
		return Program[A]{}, fmt.Errorf("decode accuracy program: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return Program[A]{}, fmt.Errorf("decode accuracy program: unexpected trailing JSON value")
		}
		return Program[A]{}, fmt.Errorf("decode accuracy program: trailing data: %w", err)
	}
	if err := validateProgram(program); err != nil {
		return Program[A]{}, err
	}
	return program, nil
}

// VerifyProgram executes a candidate and checks its observations against the
// canonical Reference behavior. Sequential calls advance every currently
// viable reference state. A parallel group explores every ordering of its calls
// and retains the successor states whose expected observations match the
// candidate observations. The candidate is correct exactly while at least one
// such reference execution remains.
//
// Candidate Invoke errors and lifecycle errors are semantic mismatches returned
// as ProgramCounterexample values. Reference errors, invalid programs,
// serialization failures, and context cancellation are verifier errors. The
// returned Trace contains all candidate evidence collected before either kind
// of failure.
func VerifyProgram[S, A, O any](
	ctx context.Context,
	program Program[A],
	reference Reference[S, A, O],
	candidate Candidate[A, O],
) (Trace[O], error) {
	trace := Trace[O]{SchemaVersion: TraceSchemaVersion, ProgramID: program.ID}
	if err := validateProgram(program); err != nil {
		return trace, err
	}
	if reference == nil {
		return trace, fmt.Errorf("accuracy program %q has no Reference", program.ID)
	}
	if (programHasEvent(program, EventCall) || programHasEvent(program, EventParallel)) && candidate.Invoke == nil {
		return trace, fmt.Errorf("accuracy program %q has no candidate Invoke function", program.ID)
	}
	if programHasEvent(program, EventCrash) && candidate.Crash == nil {
		return trace, fmt.Errorf("accuracy program %q has no candidate Crash function", program.ID)
	}
	if programHasEvent(program, EventStart) && candidate.Start == nil {
		return trace, fmt.Errorf("accuracy program %q has no candidate Start function", program.ID)
	}

	program, err := snapshotProgram(program)
	if err != nil {
		return trace, err
	}
	state, err := reference.Initial()
	if err != nil {
		return trace, fmt.Errorf("initialize reference for accuracy program %q: %w", program.ID, err)
	}
	state, err = cloneJSON(state)
	if err != nil {
		return trace, fmt.Errorf("snapshot initial reference state for accuracy program %q: %w", program.ID, err)
	}
	states := []S{state}

	for index, step := range program.Steps {
		if err := ctx.Err(); err != nil {
			return trace, fmt.Errorf("verify accuracy program %q: %w", program.ID, err)
		}
		kind := stepKind(step)
		stepTrace := TraceStep[O]{Step: index, Kind: kind}

		switch kind {
		case EventCall:
			result, invokeErr := invokeCall(ctx, candidate.Invoke, *step.Call)
			stepTrace.Calls = []CallTrace[O]{result}
			trace.Steps = append(trace.Steps, stepTrace)
			if err := ctx.Err(); err != nil {
				return trace, fmt.Errorf("verify accuracy program %q: %w", program.ID, err)
			}
			if invokeErr != nil {
				if isCandidateCallError(invokeErr) {
					return trace, programMismatch(program, trace, index, "candidate call failed")
				}
				return trace, fmt.Errorf("record candidate call for accuracy program %q at step %d: %w", program.ID, index, invokeErr)
			}
			states, err = advanceSequential(reference, states, step.Call.Action, *result.Observation)
			if err != nil {
				return trace, fmt.Errorf("apply reference for accuracy program %q at step %d: %w", program.ID, index, err)
			}
			if len(states) == 0 {
				return trace, programMismatch(program, trace, index, "candidate observation is not admitted by the reference")
			}

		case EventParallel:
			results, invokeErr := invokeParallel(ctx, candidate.Invoke, step.Parallel.Calls)
			stepTrace.Calls = results
			trace.Steps = append(trace.Steps, stepTrace)
			if err := ctx.Err(); err != nil {
				return trace, fmt.Errorf("verify accuracy program %q: %w", program.ID, err)
			}
			if invokeErr != nil {
				if isCandidateCallError(invokeErr) {
					return trace, programMismatch(program, trace, index, "one or more candidate calls failed")
				}
				return trace, fmt.Errorf("record candidate calls for accuracy program %q at step %d: %w", program.ID, index, invokeErr)
			}
			states, err = advanceParallel(ctx, reference, states, step.Parallel.Calls, results)
			if err != nil {
				return trace, fmt.Errorf("apply reference for accuracy program %q at step %d: %w", program.ID, index, err)
			}
			if len(states) == 0 {
				return trace, programMismatch(program, trace, index, "parallel observations have no legal reference serialization")
			}

		case EventCrash:
			lifecycleErr := candidate.Crash(ctx)
			if lifecycleErr != nil {
				stepTrace.Error = lifecycleErr.Error()
			}
			trace.Steps = append(trace.Steps, stepTrace)
			if err := ctx.Err(); err != nil {
				return trace, fmt.Errorf("verify accuracy program %q: %w", program.ID, err)
			}
			if lifecycleErr != nil {
				return trace, programMismatch(program, trace, index, "candidate crash failed")
			}
			states, err = advanceCrash(reference, states)
			if err != nil {
				return trace, fmt.Errorf("apply reference crash for accuracy program %q at step %d: %w", program.ID, index, err)
			}

		case EventStart:
			lifecycleErr := candidate.Start(ctx)
			if lifecycleErr != nil {
				stepTrace.Error = lifecycleErr.Error()
			}
			trace.Steps = append(trace.Steps, stepTrace)
			if err := ctx.Err(); err != nil {
				return trace, fmt.Errorf("verify accuracy program %q: %w", program.ID, err)
			}
			if lifecycleErr != nil {
				return trace, programMismatch(program, trace, index, "candidate start failed")
			}
		}
	}
	return trace, nil
}

func invokeCall[A, O any](ctx context.Context, invoke Execute[A, O], call Call[A]) (CallTrace[O], error) {
	action, err := cloneJSON(call.Action)
	if err != nil {
		return CallTrace[O]{ID: call.ID}, fmt.Errorf("snapshot candidate action %q: %w", call.ID, err)
	}
	observation, err := invoke(ctx, action)
	if err != nil {
		return CallTrace[O]{
			ID: call.ID, InvokedOrder: 1, CompletedOrder: 2, Error: err.Error(),
		}, candidateCallError{cause: err}
	}
	observation, err = cloneJSON(observation)
	if err != nil {
		return CallTrace[O]{ID: call.ID}, fmt.Errorf("snapshot candidate observation %q: %w", call.ID, err)
	}
	return CallTrace[O]{
		ID: call.ID, InvokedOrder: 1, CompletedOrder: 2, Observation: &observation,
	}, nil
}

func invokeParallel[A, O any](
	ctx context.Context,
	invoke Execute[A, O],
	calls []Call[A],
) ([]CallTrace[O], error) {
	actions := make([]A, len(calls))
	for index, call := range calls {
		action, err := cloneJSON(call.Action)
		if err != nil {
			return nil, fmt.Errorf("snapshot candidate action %q: %w", call.ID, err)
		}
		actions[index] = action
	}

	results := make([]CallTrace[O], len(calls))
	errorsByCall := make([]error, len(calls))
	ready := make(chan struct{})
	var eventOrder atomic.Int64
	var wait sync.WaitGroup
	wait.Add(len(calls))
	for index := range calls {
		go func(index int) {
			defer wait.Done()
			<-ready
			invokedOrder := eventOrder.Add(1)
			observation, err := invoke(ctx, actions[index])
			completedOrder := eventOrder.Add(1)
			if err != nil {
				results[index] = CallTrace[O]{
					ID: calls[index].ID, InvokedOrder: invokedOrder,
					CompletedOrder: completedOrder, Error: err.Error(),
				}
				errorsByCall[index] = candidateCallError{cause: err}
				return
			}
			observation, err = cloneJSON(observation)
			if err != nil {
				results[index] = CallTrace[O]{
					ID: calls[index].ID, InvokedOrder: invokedOrder, CompletedOrder: completedOrder,
				}
				errorsByCall[index] = fmt.Errorf("snapshot candidate observation %q: %w", calls[index].ID, err)
				return
			}
			results[index] = CallTrace[O]{
				ID: calls[index].ID, InvokedOrder: invokedOrder,
				CompletedOrder: completedOrder, Observation: &observation,
			}
		}(index)
	}
	close(ready)
	wait.Wait()

	for _, err := range errorsByCall {
		if err != nil {
			return results, err
		}
	}
	return results, nil
}

type candidateCallError struct {
	cause error
}

func (e candidateCallError) Error() string { return e.cause.Error() }
func (e candidateCallError) Unwrap() error { return e.cause }

func isCandidateCallError(err error) bool {
	_, ok := err.(candidateCallError)
	return ok
}

func advanceSequential[S, A, O any](
	reference Reference[S, A, O],
	states []S,
	action A,
	actual O,
) ([]S, error) {
	nextStates := make([]S, 0, len(states))
	for _, state := range states {
		stateClone, err := cloneJSON(state)
		if err != nil {
			return nil, fmt.Errorf("snapshot reference state: %w", err)
		}
		actionClone, err := cloneJSON(action)
		if err != nil {
			return nil, fmt.Errorf("snapshot reference action: %w", err)
		}
		next, expected, err := reference.Step(stateClone, actionClone)
		if err != nil {
			return nil, err
		}
		if reference.Equal(expected, actual) {
			next, err = cloneJSON(next)
			if err != nil {
				return nil, fmt.Errorf("snapshot successor reference state: %w", err)
			}
			nextStates = append(nextStates, next)
		}
	}
	return nextStates, nil
}

func advanceParallel[S, A, O any](
	ctx context.Context,
	reference Reference[S, A, O],
	states []S,
	calls []Call[A],
	results []CallTrace[O],
) ([]S, error) {
	// A literal permutation enumeration is factorial even when many prefixes
	// reach the same logical state. Memoizing the used-call set together with the
	// JSON state preserves the exact search while collapsing equivalent prefixes.
	seen := make(map[string]struct{})
	nextByState := make(map[string]S)
	used := make([]bool, len(calls))
	var explore func(S, int) error
	explore = func(state S, depth int) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		key, err := parallelSearchKey(used, state)
		if err != nil {
			return fmt.Errorf("fingerprint reference state: %w", err)
		}
		if _, exists := seen[key]; exists {
			return nil
		}
		seen[key] = struct{}{}
		if depth == len(calls) {
			stateKey, err := json.Marshal(state)
			if err != nil {
				return fmt.Errorf("fingerprint successor reference state: %w", err)
			}
			nextByState[string(stateKey)] = state
			return nil
		}

		for callIndex := range calls {
			if used[callIndex] || hasUnusedPredecessor(callIndex, used, results) {
				continue
			}
			stateClone, err := cloneJSON(state)
			if err != nil {
				return fmt.Errorf("snapshot reference state: %w", err)
			}
			action, err := cloneJSON(calls[callIndex].Action)
			if err != nil {
				return fmt.Errorf("snapshot reference action %q: %w", calls[callIndex].ID, err)
			}
			next, expected, err := reference.Step(stateClone, action)
			if err != nil {
				return err
			}
			if !reference.Equal(expected, *results[callIndex].Observation) {
				continue
			}
			next, err = cloneJSON(next)
			if err != nil {
				return fmt.Errorf("snapshot successor reference state: %w", err)
			}
			used[callIndex] = true
			if err := explore(next, depth+1); err != nil {
				return err
			}
			used[callIndex] = false
		}
		return nil
	}

	for _, initial := range states {
		state, err := cloneJSON(initial)
		if err != nil {
			return nil, fmt.Errorf("snapshot reference state: %w", err)
		}
		if err := explore(state, 0); err != nil {
			return nil, err
		}
	}
	nextStates := make([]S, 0, len(nextByState))
	for _, state := range nextByState {
		nextStates = append(nextStates, state)
	}
	return nextStates, nil
}

// hasUnusedPredecessor reports whether real-time order requires another call
// to precede the selected call. Calls overlap unless one completed before the
// other was invoked.
func hasUnusedPredecessor[O any](callIndex int, used []bool, results []CallTrace[O]) bool {
	for otherIndex, other := range results {
		if otherIndex == callIndex || used[otherIndex] {
			continue
		}
		if other.CompletedOrder < results[callIndex].InvokedOrder {
			return true
		}
	}
	return false
}

func parallelSearchKey[S any](used []bool, state S) (string, error) {
	stateJSON, err := json.Marshal(state)
	if err != nil {
		return "", err
	}
	key := make([]byte, len(used)+1+len(stateJSON))
	for index, isUsed := range used {
		if isUsed {
			key[index] = '1'
		} else {
			key[index] = '0'
		}
	}
	key[len(used)] = ':'
	copy(key[len(used)+1:], stateJSON)
	return string(key), nil
}

func advanceCrash[S, A, O any](reference Reference[S, A, O], states []S) ([]S, error) {
	nextStates := make([]S, 0, len(states))
	for _, state := range states {
		state, err := cloneJSON(state)
		if err != nil {
			return nil, fmt.Errorf("snapshot reference state: %w", err)
		}
		next, err := reference.AfterCrash(state)
		if err != nil {
			return nil, err
		}
		next, err = cloneJSON(next)
		if err != nil {
			return nil, fmt.Errorf("snapshot post-crash reference state: %w", err)
		}
		nextStates = append(nextStates, next)
	}
	return nextStates, nil
}

func programMismatch[A, O any](program Program[A], trace Trace[O], step int, reason string) error {
	prefix := program
	prefix.Steps = append([]Step[A](nil), program.Steps[:step+1]...)
	return &ProgramCounterexample[A, O]{
		SchemaVersion: ProgramCounterexampleSchemaVersion,
		Program:       prefix,
		Trace:         trace,
		Step:          step,
		Reason:        reason,
	}
}

func snapshotProgram[A any](program Program[A]) (Program[A], error) {
	encoded, err := json.Marshal(program)
	if err != nil {
		return Program[A]{}, fmt.Errorf("snapshot accuracy program %q: %w", program.ID, err)
	}
	snapshot, err := DecodeProgram[A](bytes.NewReader(encoded))
	if err != nil {
		return Program[A]{}, fmt.Errorf("snapshot accuracy program %q: %w", program.ID, err)
	}
	return snapshot, nil
}

func validateProgram[A any](program Program[A]) error {
	if program.SchemaVersion != ProgramSchemaVersion {
		return fmt.Errorf(
			"accuracy program schema_version must be %d, got %d",
			ProgramSchemaVersion,
			program.SchemaVersion,
		)
	}
	if strings.TrimSpace(program.ID) == "" {
		return fmt.Errorf("accuracy program id must not be empty")
	}
	if len(program.Steps) == 0 {
		return fmt.Errorf("accuracy program %q must contain at least one step", program.ID)
	}

	running := true
	callIDs := make(map[string]struct{})
	for index, step := range program.Steps {
		kind, err := validateStep(step)
		if err != nil {
			return fmt.Errorf("accuracy program %q step %d: %w", program.ID, index, err)
		}
		switch kind {
		case EventCall:
			if !running {
				return fmt.Errorf("accuracy program %q step %d calls a stopped candidate", program.ID, index)
			}
			if err := registerCallID(callIDs, step.Call.ID); err != nil {
				return fmt.Errorf("accuracy program %q step %d: %w", program.ID, index, err)
			}
		case EventParallel:
			if !running {
				return fmt.Errorf("accuracy program %q step %d calls a stopped candidate", program.ID, index)
			}
			if len(step.Parallel.Calls) == 0 {
				return fmt.Errorf("accuracy program %q step %d parallel group must contain at least one call", program.ID, index)
			}
			if len(step.Parallel.Calls) > MaxParallelCalls {
				return fmt.Errorf(
					"accuracy program %q step %d parallel group has %d calls, maximum is %d",
					program.ID,
					index,
					len(step.Parallel.Calls),
					MaxParallelCalls,
				)
			}
			for callIndex, call := range step.Parallel.Calls {
				if err := registerCallID(callIDs, call.ID); err != nil {
					return fmt.Errorf("accuracy program %q step %d call %d: %w", program.ID, index, callIndex, err)
				}
			}
		case EventCrash:
			if !running {
				return fmt.Errorf("accuracy program %q step %d crashes an already stopped candidate", program.ID, index)
			}
			running = false
		case EventStart:
			if running {
				return fmt.Errorf("accuracy program %q step %d starts an already running candidate", program.ID, index)
			}
			running = true
		}
	}
	return nil
}

func validateStep[A any](step Step[A]) (EventKind, error) {
	count := 0
	kind := EventKind("")
	if step.Call != nil {
		count++
		kind = EventCall
	}
	if step.Parallel != nil {
		count++
		kind = EventParallel
	}
	if step.Crash != nil {
		count++
		kind = EventCrash
	}
	if step.Start != nil {
		count++
		kind = EventStart
	}
	if count != 1 {
		return "", fmt.Errorf("must contain exactly one of call, parallel, crash, or start")
	}
	return kind, nil
}

func stepKind[A any](step Step[A]) EventKind {
	kind, _ := validateStep(step)
	return kind
}

func programHasEvent[A any](program Program[A], want EventKind) bool {
	for _, step := range program.Steps {
		if stepKind(step) == want {
			return true
		}
	}
	return false
}

func registerCallID(seen map[string]struct{}, id string) error {
	if strings.TrimSpace(id) == "" {
		return fmt.Errorf("call id must not be empty")
	}
	if _, exists := seen[id]; exists {
		return fmt.Errorf("call id %q is duplicated", id)
	}
	seen[id] = struct{}{}
	return nil
}
