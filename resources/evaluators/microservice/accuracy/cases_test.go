package accuracy

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
)

type caseAction struct {
	Delta int `json:"delta"`
}

type caseObservation struct {
	Total int `json:"total"`
}

type referenceAction struct {
	Values map[string]int `json:"values"`
}

func TestDecodeCaseStrictValidation(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  string
	}{
		{
			name:  "unknown case field",
			input: `{"schema_version":1,"id":"case","actions":[{"delta":1}],"expected":1}`,
			want:  `unknown field "expected"`,
		},
		{
			name:  "unknown action field",
			input: `{"schema_version":1,"id":"case","actions":[{"delta":1,"want":1}]}`,
			want:  `unknown field "want"`,
		},
		{
			name:  "trailing value",
			input: `{"schema_version":1,"id":"case","actions":[{"delta":1}]} {}`,
			want:  "unexpected trailing JSON value",
		},
		{
			name:  "wrong version",
			input: `{"schema_version":2,"id":"case","actions":[{"delta":1}]}`,
			want:  "schema_version must be 1, got 2",
		},
		{
			name:  "empty id",
			input: `{"schema_version":1,"id":"  ","actions":[{"delta":1}]}`,
			want:  "id must not be empty",
		},
		{
			name:  "empty actions",
			input: `{"schema_version":1,"id":"case","actions":[]}`,
			want:  "must contain at least one action",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := DecodeCase[caseAction](strings.NewReader(test.input))
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("DecodeCase() error=%v, want substring %q", err, test.want)
			}
		})
	}
}

func TestVerifyCaseExecutesInOrderWithIndependentOracle(t *testing.T) {
	testCase := Case[caseAction]{
		SchemaVersion: CaseSchemaVersion,
		ID:            "ordered",
		Actions:       []caseAction{{Delta: 3}, {Delta: -1}, {Delta: 4}},
	}
	oracleTotal := 0
	candidateTotal := 0
	var executed []int
	checks, err := VerifyCase(
		context.Background(),
		testCase,
		func(action caseAction) (caseObservation, error) {
			oracleTotal += action.Delta
			return caseObservation{Total: oracleTotal}, nil
		},
		func(_ context.Context, action caseAction) (caseObservation, error) {
			executed = append(executed, action.Delta)
			candidateTotal += action.Delta
			return caseObservation{Total: candidateTotal}, nil
		},
		func(expected, actual caseObservation) bool { return expected == actual },
	)
	if err != nil {
		t.Fatal(err)
	}
	if checks != len(testCase.Actions) {
		t.Fatalf("checks=%d, want %d", checks, len(testCase.Actions))
	}
	if !reflect.DeepEqual(executed, []int{3, -1, 4}) {
		t.Fatalf("execution order=%v", executed)
	}
	if oracleTotal != 6 || candidateTotal != 6 {
		t.Fatalf("oracle total=%d candidate total=%d", oracleTotal, candidateTotal)
	}
}

func TestVerifyCaseIsolatesReferenceBearingActionsAndHistory(t *testing.T) {
	testCase := Case[referenceAction]{
		SchemaVersion: CaseSchemaVersion,
		ID:            "reference-isolation",
		Actions: []referenceAction{
			{Values: map[string]int{"delta": 2}},
			{Values: map[string]int{"delta": 3}},
		},
	}
	oracleTotal := 0
	candidateTotal := 0
	checks, err := VerifyCase(
		context.Background(),
		testCase,
		func(action referenceAction) (caseObservation, error) {
			delta := action.Values["delta"]
			action.Values["delta"] = 1000
			oracleTotal += delta
			return caseObservation{Total: oracleTotal}, nil
		},
		func(_ context.Context, action referenceAction) (caseObservation, error) {
			delta := action.Values["delta"]
			action.Values["delta"] = 2000
			candidateTotal += delta
			if delta == 3 {
				candidateTotal++
			}
			return caseObservation{Total: candidateTotal}, nil
		},
		func(expected, actual caseObservation) bool { return expected == actual },
	)
	if checks != 2 {
		t.Fatalf("checks=%d, want 2", checks)
	}
	var counterexample *Counterexample[referenceAction, caseObservation]
	if !errors.As(err, &counterexample) {
		t.Fatalf("VerifyCase() error=%T %v, want Counterexample", err, err)
	}
	if got := counterexample.History[0].Values["delta"]; got != 2 {
		t.Fatalf("first counterexample action delta=%d, want 2", got)
	}
	if got := counterexample.History[1].Values["delta"]; got != 3 {
		t.Fatalf("second counterexample action delta=%d, want 3", got)
	}
	if got := testCase.Actions[0].Values["delta"]; got != 2 {
		t.Fatalf("caller-owned first action delta=%d, want 2", got)
	}
	if got := testCase.Actions[1].Values["delta"]; got != 3 {
		t.Fatalf("caller-owned second action delta=%d, want 3", got)
	}
}

func TestVerifyCaseRejectsNonSerializableCaseBeforeExecution(t *testing.T) {
	type invalidAction struct {
		Signal chan struct{} `json:"signal"`
	}
	applyCalls := 0
	executeCalls := 0
	checks, err := VerifyCase(
		context.Background(),
		Case[invalidAction]{
			SchemaVersion: CaseSchemaVersion,
			ID:            "not-serializable",
			Actions:       []invalidAction{{Signal: make(chan struct{})}},
		},
		func(invalidAction) (caseObservation, error) {
			applyCalls++
			return caseObservation{}, nil
		},
		func(context.Context, invalidAction) (caseObservation, error) {
			executeCalls++
			return caseObservation{}, nil
		},
		func(expected, actual caseObservation) bool { return expected == actual },
	)
	if err == nil || !strings.Contains(err.Error(), "snapshot accuracy case") {
		t.Fatalf("VerifyCase() error=%v, want snapshot error", err)
	}
	if checks != 0 || applyCalls != 0 || executeCalls != 0 {
		t.Fatalf("checks=%d applyCalls=%d executeCalls=%d", checks, applyCalls, executeCalls)
	}
}

func TestVerifyCaseHonorsContextCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	applyCalls := 0
	executeCalls := 0
	checks, err := VerifyCase(
		ctx,
		Case[caseAction]{SchemaVersion: CaseSchemaVersion, ID: "cancelled", Actions: []caseAction{{Delta: 1}}},
		func(caseAction) (caseObservation, error) {
			applyCalls++
			return caseObservation{}, nil
		},
		func(context.Context, caseAction) (caseObservation, error) {
			executeCalls++
			return caseObservation{}, nil
		},
		func(expected, actual caseObservation) bool { return expected == actual },
	)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("VerifyCase() error=%v, want context.Canceled", err)
	}
	if checks != 0 || applyCalls != 0 || executeCalls != 0 {
		t.Fatalf("checks=%d applyCalls=%d executeCalls=%d", checks, applyCalls, executeCalls)
	}
}

func TestVerifyCaseEmitsStableVersionedCounterexample(t *testing.T) {
	testCase := Case[caseAction]{
		SchemaVersion: CaseSchemaVersion,
		ID:            "mismatch",
		Actions:       []caseAction{{Delta: 2}, {Delta: 3}, {Delta: 9}},
	}
	oracleTotal := 0
	candidateTotal := 0
	checks, err := VerifyCase(
		context.Background(),
		testCase,
		func(action caseAction) (caseObservation, error) {
			oracleTotal += action.Delta
			return caseObservation{Total: oracleTotal}, nil
		},
		func(_ context.Context, action caseAction) (caseObservation, error) {
			candidateTotal += action.Delta
			if action.Delta == 3 {
				candidateTotal--
			}
			return caseObservation{Total: candidateTotal}, nil
		},
		func(expected, actual caseObservation) bool { return expected == actual },
	)
	if checks != 2 {
		t.Fatalf("checks=%d, want 2", checks)
	}
	var counterexample *Counterexample[caseAction, caseObservation]
	if !errors.As(err, &counterexample) {
		t.Fatalf("VerifyCase() error=%T %v, want Counterexample", err, err)
	}
	want := `accuracy case mismatch: {"schema_version":1,"case_id":"mismatch","step":1,"history":[{"delta":2},{"delta":3}],"expected":{"total":5},"actual":{"total":4}}`
	if err.Error() != want {
		t.Fatalf("counterexample:\n got %s\nwant %s", err, want)
	}
	if len(counterexample.History) != 2 || counterexample.Step != 1 {
		t.Fatalf("counterexample=%+v", counterexample)
	}
}

func TestDecodedCaseReplaysExactActions(t *testing.T) {
	original := Case[caseAction]{
		SchemaVersion: CaseSchemaVersion,
		ID:            "replay",
		Actions:       []caseAction{{Delta: 7}, {Delta: -2}, {Delta: 5}},
	}
	encoded, err := json.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	replay, err := DecodeCase[caseAction](strings.NewReader(string(encoded)))
	if err != nil {
		t.Fatal(err)
	}
	var executed []caseAction
	total := 0
	checks, err := VerifyCase(
		context.Background(),
		replay,
		func(action caseAction) (caseObservation, error) {
			total += action.Delta
			return caseObservation{Total: total}, nil
		},
		func(_ context.Context, action caseAction) (caseObservation, error) {
			executed = append(executed, action)
			candidateTotal := 0
			for _, item := range executed {
				candidateTotal += item.Delta
			}
			return caseObservation{Total: candidateTotal}, nil
		},
		func(expected, actual caseObservation) bool { return expected == actual },
	)
	if err != nil {
		t.Fatal(err)
	}
	if checks != len(original.Actions) || !reflect.DeepEqual(executed, original.Actions) {
		t.Fatalf("checks=%d replayed=%v want=%v", checks, executed, original.Actions)
	}
}

func TestVerifyCaseReportsCandidateExecutionError(t *testing.T) {
	checks, err := VerifyCase(
		context.Background(),
		Case[caseAction]{SchemaVersion: CaseSchemaVersion, ID: "transport", Actions: []caseAction{{Delta: 1}}},
		func(action caseAction) (caseObservation, error) {
			return caseObservation{Total: action.Delta}, nil
		},
		func(context.Context, caseAction) (caseObservation, error) {
			return caseObservation{}, errors.New("connection lost")
		},
		func(expected, actual caseObservation) bool { return expected == actual },
	)
	if checks != 1 {
		t.Fatalf("checks=%d, want 1", checks)
	}
	var counterexample *Counterexample[caseAction, caseObservation]
	if !errors.As(err, &counterexample) {
		t.Fatalf("VerifyCase() error=%T %v, want Counterexample", err, err)
	}
	if counterexample.Actual != nil || counterexample.ActualError != "connection lost" {
		t.Fatalf("counterexample=%+v", counterexample)
	}
}
