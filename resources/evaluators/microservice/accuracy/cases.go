package accuracy

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"strings"
)

const (
	CaseSchemaVersion           = 1
	CounterexampleSchemaVersion = 1
)

// Case is a replayable sequence of executable inputs. Expected observations
// deliberately do not belong in this contract; an independent Apply function
// derives them from the actions.
type Case[A any] struct {
	SchemaVersion int    `json:"schema_version"`
	ID            string `json:"id"`
	Actions       []A    `json:"actions"`
}

// Apply advances an independent oracle model by one action.
type Apply[A, O any] func(A) (O, error)

// Execute applies one action to the candidate and returns its observation.
type Execute[A, O any] func(context.Context, A) (O, error)

// Equal compares an oracle observation with a candidate observation.
type Equal[O any] func(O, O) bool

// Counterexample reports the shortest executed prefix that reaches the first
// candidate error or semantic mismatch.
type Counterexample[A, O any] struct {
	SchemaVersion int    `json:"schema_version"`
	CaseID        string `json:"case_id"`
	Step          int    `json:"step"`
	History       []A    `json:"history"`
	Expected      O      `json:"expected"`
	Actual        *O     `json:"actual,omitempty"`
	ActualError   string `json:"actual_error,omitempty"`
}

func (c *Counterexample[A, O]) Error() string {
	encoded, err := json.Marshal(c)
	if err != nil {
		return fmt.Sprintf("accuracy case %q mismatch at step %d", c.CaseID, c.Step)
	}
	return "accuracy case mismatch: " + string(encoded)
}

// DecodeCase strictly decodes one case. Unknown fields and trailing JSON are
// rejected so a replay cannot silently reinterpret a persisted input.
func DecodeCase[A any](input io.Reader) (Case[A], error) {
	decoder := json.NewDecoder(input)
	decoder.DisallowUnknownFields()
	var replay Case[A]
	if err := decoder.Decode(&replay); err != nil {
		return Case[A]{}, fmt.Errorf("decode accuracy case: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return Case[A]{}, fmt.Errorf("decode accuracy case: unexpected trailing JSON value")
		}
		return Case[A]{}, fmt.Errorf("decode accuracy case: trailing data: %w", err)
	}
	if err := validateCase(replay); err != nil {
		return Case[A]{}, err
	}
	return replay, nil
}

// VerifyCase executes actions in declaration order and compares each candidate
// observation with an independently derived oracle observation. The returned
// count is the number of candidate actions attempted.
func VerifyCase[A, O any](
	ctx context.Context,
	testCase Case[A],
	apply Apply[A, O],
	execute Execute[A, O],
	equal Equal[O],
) (int, error) {
	if err := validateCase(testCase); err != nil {
		return 0, err
	}
	if apply == nil {
		return 0, fmt.Errorf("accuracy case %q has no oracle Apply function", testCase.ID)
	}
	if execute == nil {
		return 0, fmt.Errorf("accuracy case %q has no candidate Execute function", testCase.ID)
	}
	if equal == nil {
		return 0, fmt.Errorf("accuracy case %q has no observation Equal function", testCase.ID)
	}
	testCase, err := snapshotCase(testCase)
	if err != nil {
		return 0, err
	}

	for index, action := range testCase.Actions {
		if err := ctx.Err(); err != nil {
			return index, fmt.Errorf("verify accuracy case %q: %w", testCase.ID, err)
		}
		oracleAction, err := cloneJSON(action)
		if err != nil {
			return index, fmt.Errorf(
				"snapshot oracle action for accuracy case %q at step %d: %w",
				testCase.ID,
				index,
				err,
			)
		}
		expected, err := apply(oracleAction)
		if err != nil {
			return index, fmt.Errorf(
				"apply accuracy oracle for case %q at step %d: %w",
				testCase.ID,
				index,
				err,
			)
		}
		if err := ctx.Err(); err != nil {
			return index, fmt.Errorf("verify accuracy case %q: %w", testCase.ID, err)
		}
		candidateAction, err := cloneJSON(action)
		if err != nil {
			return index, fmt.Errorf(
				"snapshot candidate action for accuracy case %q at step %d: %w",
				testCase.ID,
				index,
				err,
			)
		}
		actual, executeErr := execute(ctx, candidateAction)
		attempted := index + 1
		if executeErr != nil {
			if err := ctx.Err(); err != nil {
				return attempted, fmt.Errorf("verify accuracy case %q: %w", testCase.ID, err)
			}
			return attempted, &Counterexample[A, O]{
				SchemaVersion: CounterexampleSchemaVersion,
				CaseID:        testCase.ID,
				Step:          index,
				History:       append([]A(nil), testCase.Actions[:attempted]...),
				Expected:      expected,
				ActualError:   executeErr.Error(),
			}
		}
		if !equal(expected, actual) {
			return attempted, &Counterexample[A, O]{
				SchemaVersion: CounterexampleSchemaVersion,
				CaseID:        testCase.ID,
				Step:          index,
				History:       append([]A(nil), testCase.Actions[:attempted]...),
				Expected:      expected,
				Actual:        &actual,
			}
		}
	}
	return len(testCase.Actions), nil
}

func snapshotCase[A any](testCase Case[A]) (Case[A], error) {
	encoded, err := json.Marshal(testCase)
	if err != nil {
		return Case[A]{}, fmt.Errorf("snapshot accuracy case %q: %w", testCase.ID, err)
	}
	snapshot, err := DecodeCase[A](bytes.NewReader(encoded))
	if err != nil {
		return Case[A]{}, fmt.Errorf("snapshot accuracy case %q: %w", testCase.ID, err)
	}
	return snapshot, nil
}

func cloneJSON[T any](value T) (T, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		var zero T
		return zero, err
	}
	var clone T
	if err := json.Unmarshal(encoded, &clone); err != nil {
		var zero T
		return zero, err
	}
	return clone, nil
}

func validateCase[A any](testCase Case[A]) error {
	if testCase.SchemaVersion != CaseSchemaVersion {
		return fmt.Errorf(
			"accuracy case schema_version must be %d, got %d",
			CaseSchemaVersion,
			testCase.SchemaVersion,
		)
	}
	if strings.TrimSpace(testCase.ID) == "" {
		return fmt.Errorf("accuracy case id must not be empty")
	}
	if len(testCase.Actions) == 0 {
		return fmt.Errorf("accuracy case %q must contain at least one action", testCase.ID)
	}
	return nil
}
