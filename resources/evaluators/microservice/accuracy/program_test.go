package accuracy

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"sync"
	"testing"
)

type programAction struct {
	Kind  string `json:"kind"`
	Value int    `json:"value,omitempty"`
}

type programObservation struct {
	Value   int  `json:"value"`
	Applied bool `json:"applied,omitempty"`
}

type invalidProgramAction struct {
	Signal chan struct{} `json:"signal"`
}

type invalidProgramReference struct{}

func (invalidProgramReference) Initial() (struct{}, error) { return struct{}{}, nil }
func (invalidProgramReference) Step(
	struct{},
	invalidProgramAction,
) (struct{}, struct{}, error) {
	return struct{}{}, struct{}{}, nil
}
func (invalidProgramReference) AfterCrash(state struct{}) (struct{}, error) { return state, nil }
func (invalidProgramReference) Equal(struct{}, struct{}) bool               { return true }

type integerReference struct{}

func (integerReference) Initial() (int, error) { return 0, nil }

func (integerReference) Step(state int, action programAction) (int, programObservation, error) {
	switch action.Kind {
	case "add":
		state += action.Value
		return state, programObservation{Value: state}, nil
	case "take":
		if state >= action.Value {
			state -= action.Value
			return state, programObservation{Value: state, Applied: true}, nil
		}
		return state, programObservation{Value: state}, nil
	case "read":
		return state, programObservation{Value: state}, nil
	default:
		return 0, programObservation{}, errors.New("unknown reference action")
	}
}

func (integerReference) AfterCrash(state int) (int, error) { return state, nil }

func (integerReference) Equal(expected, actual programObservation) bool {
	return expected == actual
}

type volatileIntegerReference struct{ integerReference }

func (volatileIntegerReference) AfterCrash(int) (int, error) { return 0, nil }

type countingReference struct {
	steps int
}

func (*countingReference) Initial() (int, error) { return 0, nil }
func (r *countingReference) Step(state int, _ programAction) (int, programObservation, error) {
	r.steps++
	return state, programObservation{Value: state}, nil
}
func (*countingReference) AfterCrash(state int) (int, error) { return state, nil }
func (*countingReference) Equal(expected, actual programObservation) bool {
	return expected == actual
}

func TestDecodeProgramStrictValidation(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  string
	}{
		{
			name:  "unknown program field",
			input: `{"schema_version":1,"id":"p","steps":[{"call":{"id":"a","action":{"kind":"read"}}}],"oracle":{}}`,
			want:  `unknown field "oracle"`,
		},
		{
			name:  "unknown action field",
			input: `{"schema_version":1,"id":"p","steps":[{"call":{"id":"a","action":{"kind":"read","extra":1}}}]}`,
			want:  `unknown field "extra"`,
		},
		{
			name:  "unknown crash field",
			input: `{"schema_version":1,"id":"p","steps":[{"crash":{"delay":1}}]}`,
			want:  `unknown field "delay"`,
		},
		{
			name:  "trailing value",
			input: `{"schema_version":1,"id":"p","steps":[{"call":{"id":"a","action":{"kind":"read"}}}]} {}`,
			want:  "unexpected trailing JSON value",
		},
		{
			name:  "wrong version",
			input: `{"schema_version":2,"id":"p","steps":[{"call":{"id":"a","action":{"kind":"read"}}}]}`,
			want:  "schema_version must be 1, got 2",
		},
		{
			name:  "empty program id",
			input: `{"schema_version":1,"id":" ","steps":[{"call":{"id":"a","action":{"kind":"read"}}}]}`,
			want:  "id must not be empty",
		},
		{
			name:  "empty steps",
			input: `{"schema_version":1,"id":"p","steps":[]}`,
			want:  "at least one step",
		},
		{
			name:  "empty event union",
			input: `{"schema_version":1,"id":"p","steps":[{}]}`,
			want:  "exactly one",
		},
		{
			name:  "multiple event variants",
			input: `{"schema_version":1,"id":"p","steps":[{"call":{"id":"a","action":{"kind":"read"}},"crash":{}}]}`,
			want:  "exactly one",
		},
		{
			name:  "empty parallel group",
			input: `{"schema_version":1,"id":"p","steps":[{"parallel":{"calls":[]}}]}`,
			want:  "at least one call",
		},
		{
			name: "parallel group too wide",
			input: `{"schema_version":1,"id":"p","steps":[{"parallel":{"calls":[` +
				`{"id":"1","action":{"kind":"read"}},` +
				`{"id":"2","action":{"kind":"read"}},` +
				`{"id":"3","action":{"kind":"read"}},` +
				`{"id":"4","action":{"kind":"read"}},` +
				`{"id":"5","action":{"kind":"read"}},` +
				`{"id":"6","action":{"kind":"read"}},` +
				`{"id":"7","action":{"kind":"read"}},` +
				`{"id":"8","action":{"kind":"read"}},` +
				`{"id":"9","action":{"kind":"read"}}]}}]}`,
			want: "9 calls, maximum is 8",
		},
		{
			name:  "empty call id",
			input: `{"schema_version":1,"id":"p","steps":[{"call":{"id":" ","action":{"kind":"read"}}}]}`,
			want:  "call id must not be empty",
		},
		{
			name:  "duplicate call id",
			input: `{"schema_version":1,"id":"p","steps":[{"call":{"id":"a","action":{"kind":"read"}}},{"parallel":{"calls":[{"id":"a","action":{"kind":"read"}}]}}]}`,
			want:  `call id "a" is duplicated`,
		},
		{
			name:  "call while stopped",
			input: `{"schema_version":1,"id":"p","steps":[{"crash":{}},{"call":{"id":"a","action":{"kind":"read"}}}]}`,
			want:  "calls a stopped candidate",
		},
		{
			name:  "start while running",
			input: `{"schema_version":1,"id":"p","steps":[{"start":{}}]}`,
			want:  "starts an already running candidate",
		},
		{
			name:  "repeated crash",
			input: `{"schema_version":1,"id":"p","steps":[{"crash":{}},{"crash":{}}]}`,
			want:  "crashes an already stopped candidate",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := DecodeProgram[programAction](strings.NewReader(test.input))
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("DecodeProgram() error=%v, want substring %q", err, test.want)
			}
		})
	}
}

func TestProgramJSONRoundTrip(t *testing.T) {
	original := Program[programAction]{
		SchemaVersion: ProgramSchemaVersion,
		ID:            "round-trip",
		Steps: []Step[programAction]{
			{Call: &Call[programAction]{ID: "one", Action: programAction{Kind: "read"}}},
			{Parallel: &Parallel[programAction]{Calls: []Call[programAction]{
				{ID: "two", Action: programAction{Kind: "add", Value: 1}},
				{ID: "three", Action: programAction{Kind: "add", Value: 2}},
			}}},
			{Crash: &Crash{}},
			{Start: &Start{}},
		},
	}
	encoded, err := json.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := DecodeProgram[programAction](strings.NewReader(string(encoded)))
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(decoded, original) {
		t.Fatalf("decoded=%+v, want %+v", decoded, original)
	}
}

func TestVerifyProgramSequentialAndLifecycle(t *testing.T) {
	program := Program[programAction]{
		SchemaVersion: ProgramSchemaVersion,
		ID:            "lifecycle",
		Steps: []Step[programAction]{
			{Call: &Call[programAction]{ID: "add", Action: programAction{Kind: "add", Value: 3}}},
			{Crash: &Crash{}},
			{Start: &Start{}},
			{Call: &Call[programAction]{ID: "read", Action: programAction{Kind: "read"}}},
		},
	}
	candidateState := 0
	var lifecycle []string
	trace, err := VerifyProgram(
		context.Background(),
		program,
		integerReference{},
		Candidate[programAction, programObservation]{
			Invoke: func(_ context.Context, action programAction) (programObservation, error) {
				switch action.Kind {
				case "add":
					candidateState += action.Value
				case "read":
				default:
					return programObservation{}, errors.New("unexpected action")
				}
				return programObservation{Value: candidateState}, nil
			},
			Crash: func(context.Context) error {
				lifecycle = append(lifecycle, "crash")
				return nil
			},
			Start: func(context.Context) error {
				lifecycle = append(lifecycle, "start")
				return nil
			},
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(lifecycle, []string{"crash", "start"}) {
		t.Fatalf("lifecycle=%v", lifecycle)
	}
	if len(trace.Steps) != 4 || trace.Steps[3].Calls[0].Observation.Value != 3 {
		t.Fatalf("trace=%+v", trace)
	}
}

func TestVerifyProgramAppliesReferenceCrashSemantics(t *testing.T) {
	program := Program[programAction]{
		SchemaVersion: ProgramSchemaVersion,
		ID:            "volatile-state",
		Steps: []Step[programAction]{
			{Call: &Call[programAction]{ID: "add", Action: programAction{Kind: "add", Value: 3}}},
			{Crash: &Crash{}},
			{Start: &Start{}},
			{Call: &Call[programAction]{ID: "read", Action: programAction{Kind: "read"}}},
		},
	}
	candidateState := 0
	_, err := VerifyProgram(
		context.Background(),
		program,
		volatileIntegerReference{},
		Candidate[programAction, programObservation]{
			Invoke: func(_ context.Context, action programAction) (programObservation, error) {
				if action.Kind == "add" {
					candidateState += action.Value
				}
				return programObservation{Value: candidateState}, nil
			},
			Crash: func(context.Context) error {
				candidateState = 0
				return nil
			},
			Start: func(context.Context) error { return nil },
		},
	)
	if err != nil {
		t.Fatal(err)
	}
}

func TestVerifyProgramLifecycleErrorIsCounterexample(t *testing.T) {
	program := Program[programAction]{
		SchemaVersion: ProgramSchemaVersion,
		ID:            "crash-failure",
		Steps: []Step[programAction]{
			{Crash: &Crash{}},
			{Start: &Start{}},
		},
	}
	trace, err := VerifyProgram(
		context.Background(),
		program,
		integerReference{},
		Candidate[programAction, programObservation]{
			Crash: func(context.Context) error { return errors.New("not stopped") },
			Start: func(context.Context) error { return nil },
		},
	)
	var counterexample *ProgramCounterexample[programAction, programObservation]
	if !errors.As(err, &counterexample) {
		t.Fatalf("VerifyProgram() error=%T %v, want ProgramCounterexample", err, err)
	}
	if len(trace.Steps) != 1 || trace.Steps[0].Error != "not stopped" || len(counterexample.Program.Steps) != 1 {
		t.Fatalf("trace=%+v counterexample=%+v", trace, counterexample)
	}
}

func TestVerifyProgramRequiresOnlyHooksUsedByProgram(t *testing.T) {
	tests := []struct {
		name      string
		program   Program[programAction]
		candidate Candidate[programAction, programObservation]
		want      string
	}{
		{
			name: "call requires invoke",
			program: Program[programAction]{
				SchemaVersion: ProgramSchemaVersion,
				ID:            "missing-invoke",
				Steps: []Step[programAction]{
					{Call: &Call[programAction]{ID: "read", Action: programAction{Kind: "read"}}},
				},
			},
			want: "no candidate Invoke function",
		},
		{
			name: "crash requires crash",
			program: Program[programAction]{
				SchemaVersion: ProgramSchemaVersion,
				ID:            "missing-crash",
				Steps: []Step[programAction]{
					{Crash: &Crash{}},
				},
			},
			want: "no candidate Crash function",
		},
		{
			name: "start requires start",
			program: Program[programAction]{
				SchemaVersion: ProgramSchemaVersion,
				ID:            "missing-start",
				Steps: []Step[programAction]{
					{Crash: &Crash{}},
					{Start: &Start{}},
				},
			},
			candidate: Candidate[programAction, programObservation]{
				Crash: func(context.Context) error { return nil },
			},
			want: "no candidate Start function",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := VerifyProgram(context.Background(), test.program, integerReference{}, test.candidate)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("VerifyProgram() error=%v, want substring %q", err, test.want)
			}
		})
	}
}

func TestVerifyProgramAcceptsAnyLegalParallelSerialization(t *testing.T) {
	program := Program[programAction]{
		SchemaVersion: ProgramSchemaVersion,
		ID:            "parallel",
		Steps: []Step[programAction]{
			{Call: &Call[programAction]{ID: "seed", Action: programAction{Kind: "add", Value: 1}}},
			{Parallel: &Parallel[programAction]{Calls: []Call[programAction]{
				{ID: "left", Action: programAction{Kind: "take", Value: 1}},
				{ID: "right", Action: programAction{Kind: "take", Value: 1}},
			}}},
			{Call: &Call[programAction]{ID: "read", Action: programAction{Kind: "read"}}},
		},
	}
	state := 0
	var lock sync.Mutex
	trace, err := VerifyProgram(
		context.Background(),
		program,
		integerReference{},
		Candidate[programAction, programObservation]{
			Invoke: func(_ context.Context, action programAction) (programObservation, error) {
				lock.Lock()
				defer lock.Unlock()
				switch action.Kind {
				case "add":
					state += action.Value
					return programObservation{Value: state}, nil
				case "take":
					if state >= action.Value {
						state -= action.Value
						return programObservation{Value: state, Applied: true}, nil
					}
					return programObservation{Value: state}, nil
				case "read":
					return programObservation{Value: state}, nil
				default:
					return programObservation{}, errors.New("unexpected action")
				}
			},
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if got := trace.Steps[1]; got.Kind != EventParallel || len(got.Calls) != 2 || got.Calls[0].ID != "left" || got.Calls[1].ID != "right" {
		t.Fatalf("parallel trace=%+v", got)
	}
}

func TestVerifyProgramMemoizesEquivalentParallelPrefixes(t *testing.T) {
	calls := make([]Call[programAction], MaxParallelCalls)
	for index := range calls {
		calls[index] = Call[programAction]{
			ID:     string(rune('a' + index)),
			Action: programAction{Kind: "read"},
		}
	}
	reference := &countingReference{}
	_, err := VerifyProgram(
		context.Background(),
		Program[programAction]{
			SchemaVersion: ProgramSchemaVersion,
			ID:            "memoized",
			Steps: []Step[programAction]{
				{Parallel: &Parallel[programAction]{Calls: calls}},
			},
		},
		reference,
		Candidate[programAction, programObservation]{
			Invoke: func(context.Context, programAction) (programObservation, error) {
				return programObservation{}, nil
			},
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	// There are only 2^8 used-call sets when every ordering reaches the same
	// state. A raw permutation search would call Step more than 300,000 times.
	if reference.steps > 1100 {
		t.Fatalf("reference Step calls=%d, memoization did not collapse equivalent prefixes", reference.steps)
	}
}

func TestParallelSearchRespectsInvocationCompletionPrecedence(t *testing.T) {
	calls := []Call[programAction]{
		{ID: "left", Action: programAction{Kind: "add", Value: 1}},
		{ID: "right", Action: programAction{Kind: "add", Value: 2}},
	}
	// These observations can only be explained by right then left. The trace
	// says left completed before right was invoked, so that serialization is
	// illegal even though both calls belong to the same parallel step.
	results := []CallTrace[programObservation]{
		{
			ID: "left", InvokedOrder: 1, CompletedOrder: 2,
			Observation: &programObservation{Value: 3},
		},
		{
			ID: "right", InvokedOrder: 3, CompletedOrder: 4,
			Observation: &programObservation{Value: 2},
		},
	}
	states, err := advanceParallel(context.Background(), integerReference{}, []int{0}, calls, results)
	if err != nil {
		t.Fatal(err)
	}
	if len(states) != 0 {
		t.Fatalf("advanceParallel() states=%v, want no legal real-time serialization", states)
	}

	// When the calls overlap, the same observations admit right then left.
	results[0].CompletedOrder = 4
	results[1].InvokedOrder = 2
	results[1].CompletedOrder = 3
	states, err = advanceParallel(context.Background(), integerReference{}, []int{0}, calls, results)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(states, []int{3}) {
		t.Fatalf("advanceParallel() states=%v, want [3]", states)
	}
}

func TestVerifyProgramReportsParallelCounterexamplePrefix(t *testing.T) {
	program := Program[programAction]{
		SchemaVersion: ProgramSchemaVersion,
		ID:            "bad-parallel",
		Steps: []Step[programAction]{
			{Call: &Call[programAction]{ID: "seed", Action: programAction{Kind: "add", Value: 1}}},
			{Parallel: &Parallel[programAction]{Calls: []Call[programAction]{
				{ID: "left", Action: programAction{Kind: "take", Value: 1}},
				{ID: "right", Action: programAction{Kind: "take", Value: 1}},
			}}},
			{Call: &Call[programAction]{ID: "unreached", Action: programAction{Kind: "read"}}},
		},
	}
	trace, err := VerifyProgram(
		context.Background(),
		program,
		integerReference{},
		Candidate[programAction, programObservation]{
			Invoke: func(_ context.Context, action programAction) (programObservation, error) {
				if action.Kind == "add" {
					return programObservation{Value: 1}, nil
				}
				return programObservation{Value: 0, Applied: true}, nil
			},
		},
	)
	if len(trace.Steps) != 2 {
		t.Fatalf("trace steps=%d, want 2", len(trace.Steps))
	}
	var counterexample *ProgramCounterexample[programAction, programObservation]
	if !errors.As(err, &counterexample) {
		t.Fatalf("VerifyProgram() error=%T %v, want ProgramCounterexample", err, err)
	}
	if counterexample.Step != 1 || len(counterexample.Program.Steps) != 2 || len(counterexample.Trace.Steps) != 2 {
		t.Fatalf("counterexample=%+v", counterexample)
	}
	if !strings.Contains(counterexample.Error(), `"reason":"parallel observations have no legal reference serialization"`) {
		t.Fatalf("counterexample error=%s", counterexample.Error())
	}
}

func TestVerifyProgramCandidateErrorIsCounterexample(t *testing.T) {
	program := Program[programAction]{
		SchemaVersion: ProgramSchemaVersion,
		ID:            "transport",
		Steps: []Step[programAction]{
			{Call: &Call[programAction]{ID: "read", Action: programAction{Kind: "read"}}},
		},
	}
	trace, err := VerifyProgram(
		context.Background(),
		program,
		integerReference{},
		Candidate[programAction, programObservation]{
			Invoke: func(context.Context, programAction) (programObservation, error) {
				return programObservation{}, errors.New("connection lost")
			},
		},
	)
	var counterexample *ProgramCounterexample[programAction, programObservation]
	if !errors.As(err, &counterexample) {
		t.Fatalf("VerifyProgram() error=%T %v, want ProgramCounterexample", err, err)
	}
	if len(trace.Steps) != 1 || trace.Steps[0].Calls[0].Error != "connection lost" {
		t.Fatalf("trace=%+v", trace)
	}
}

func TestVerifyProgramRejectsNonSerializableInputBeforeExecution(t *testing.T) {
	invocations := 0
	trace, err := VerifyProgram(
		context.Background(),
		Program[invalidProgramAction]{
			SchemaVersion: ProgramSchemaVersion,
			ID:            "invalid",
			Steps: []Step[invalidProgramAction]{
				{Call: &Call[invalidProgramAction]{
					ID:     "call",
					Action: invalidProgramAction{Signal: make(chan struct{})},
				}},
			},
		},
		invalidProgramReference{},
		Candidate[invalidProgramAction, struct{}]{
			Invoke: func(context.Context, invalidProgramAction) (struct{}, error) {
				invocations++
				return struct{}{}, nil
			},
		},
	)
	if err == nil || !strings.Contains(err.Error(), "snapshot accuracy program") {
		t.Fatalf("VerifyProgram() error=%v, want snapshot error", err)
	}
	if len(trace.Steps) != 0 || invocations != 0 {
		t.Fatalf("trace=%+v invocations=%d", trace, invocations)
	}
}

func TestVerifyProgramHonorsCancelledContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	invocations := 0
	trace, err := VerifyProgram(
		ctx,
		Program[programAction]{
			SchemaVersion: ProgramSchemaVersion,
			ID:            "cancelled",
			Steps: []Step[programAction]{
				{Call: &Call[programAction]{ID: "read", Action: programAction{Kind: "read"}}},
			},
		},
		integerReference{},
		Candidate[programAction, programObservation]{
			Invoke: func(context.Context, programAction) (programObservation, error) {
				invocations++
				return programObservation{}, nil
			},
		},
	)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("VerifyProgram() error=%v, want context.Canceled", err)
	}
	if len(trace.Steps) != 0 || invocations != 0 {
		t.Fatalf("trace=%+v invocations=%d", trace, invocations)
	}
}
