# Event programs and reference oracles

This package provides application-neutral machinery for checking a running
microservice against an executable reference model. An application supplies:

1. a replayable `Program[A]` describing calls, concurrency, and lifecycle
   events;
2. a `Reference[S, A, O]` defining the application's canonical state
   transitions and observations; and
3. a `Candidate[A, O]` translating actions and lifecycle events into operations
   against the system under test.

The framework schedules the program, records the candidate trace, explores
legal reference executions, compares observations, and reports a replayable
counterexample. It does not know endpoint paths, request schemas, fixture
catalogs, or application invariants.

## Correctness contract

For a program containing only sequential calls, a candidate is correct when
every observed result equals the result produced by applying the same action to
the reference state reached by the preceding calls.

For a program containing parallel calls, a candidate is correct when there
exists a sequential ordering of those calls that:

- is admitted by the reference model;
- produces an equivalent observation for every call; and
- reaches a reference state from which the remainder of the program is also
  correct.

This is a linearizability check at each declared parallel barrier. The verifier
records invocation and completion order, then retains all distinct matching
successor states rather than committing to the first matching ordering. A
locally valid ordering may otherwise make a later call fail even though another
valid ordering explains the complete execution.

Calls in different program steps are ordered. Calls in the same parallel step
are launched together. Overlapping calls are unordered for reference-model
purposes, while a call that completes before another is invoked must precede
it. Version 1 does not describe user-declared dependencies, partial orders, or
operations that remain in flight across a lifecycle event.

## Public model

### Event programs

```go
type Program[A any] struct {
    SchemaVersion int       `json:"schema_version"`
    ID            string    `json:"id"`
    Steps         []Step[A] `json:"steps"`
}

type Step[A any] struct {
    Call     *Call[A]     `json:"call,omitempty"`
    Parallel *Parallel[A] `json:"parallel,omitempty"`
    Crash    *Crash       `json:"crash,omitempty"`
    Start    *Start       `json:"start,omitempty"`
}

type Call[A any] struct {
    ID     string `json:"id"`
    Action A      `json:"action"`
}

type Parallel[A any] struct {
    Calls []Call[A] `json:"calls"`
}
```

Exactly one member of each `Step` must be present. Call IDs are unique across
the entire program, which lets traces and counterexamples identify an operation
without depending on scheduling order.

Programs are data, not callbacks. A generator may use any strategy, but its
output contract is simply `Program[A]`. Persisting that value captures the
stimuli needed for replay without rerunning the generator or storing expected
answers beside the test inputs.

There is deliberately no generator interface in the framework. An application
can expose a function shaped for its own fixtures and input policy, for example:

```go
func Generate(seed int64, cases int, catalog Catalog) (accuracy.Program[Action], error)
```

The function should be deterministic for its explicit inputs. The serialized
program, not this function signature or its seed, is the event-input replay
contract. Reproduction still requires the application to establish the same
initial candidate fixtures and construct the same version of its reference
model. Those application-owned inputs are not hidden inside the framework
schema.

`DecodeProgram` rejects unknown fields, trailing JSON, unsupported schema
versions, blank program or call IDs, empty programs and parallel groups,
parallel groups wider than `MaxParallelCalls`, multiple event variants in one
step, and invalid lifecycle sequences. `MaxParallelCalls` is currently 8. A
program starts with the candidate running. Calls and crashes require the
running state; starts require the stopped state.

### Reference behavior

```go
type Reference[S, A, O any] interface {
    Initial() (S, error)
    Step(S, A) (next S, expected O, err error)
    AfterCrash(S) (S, error)
    Equal(expected, actual O) bool
}
```

`S` is logical application state, `A` is an application operation, and `O` is
the normalized externally visible result. The methods mean:

- `Initial` constructs fresh reference state for one verification.
- `Step` defines the required response and next state for one atomic action.
- `AfterCrash` defines which acknowledged state survives a crash.
- `Equal` compares a reference observation with a normalized candidate
  observation. It should ignore representation differences that are not part
  of application semantics.

The reference never issues requests and should not inspect candidate state. It
is the canonical application specification used for sequential, concurrent,
and durability checks. For example, a service promising durable acknowledged
writes returns those writes unchanged from `AfterCrash`; a service explicitly
allowing volatile writes can remove them there.

`Step` is deterministic in version 1. Concurrency can still have multiple legal
outcomes because the framework explores the possible serial orders. Semantics
that are nondeterministic even for a fixed state and action need a future
reference contract.

All logical mutable state must be represented in the JSON encoding of `S`.
`Step`, `AfterCrash`, and `Equal` must not depend on hidden mutable receiver
state, and `Equal` must not mutate either observation. The exact concurrency
search memoizes branches by the used call set and serialized `S`, so states with
identical JSON are required to have identical future behavior.

### Candidate adapter

```go
type Candidate[A, O any] struct {
    Invoke Execute[A, O]
    Crash  func(context.Context) error
    Start  func(context.Context) error
}
```

`Invoke` converts an application action into a real request and normalizes its
response into `O`. It may be called concurrently for a parallel step, so the
adapter and its underlying runtime must be safe for concurrent use. It may be
nil for a lifecycle-only program. `Crash` and `Start` operate the candidate
lifecycle and may be nil when the program does not contain the corresponding
event.

The adapter owns protocol details. For HTTP, it chooses the method and path,
encodes the request, enforces application response schemas, and returns a typed
observation. The framework owns neither those mappings nor the meaning of the
observation.

An `Invoke` error means the adapter or transport could not produce an
observation and is an immediate candidate mismatch. An application-level
rejection, such as insufficient capacity, is observable behavior and must be
represented in `O` so the reference can accept or reject it. Candidate crash or
start errors are also mismatches. Invalid programs, reference errors,
serialization failures, and context cancellation are verifier failures rather
than claims about application correctness.

## Execution semantics

`VerifyProgram(ctx, program, reference, candidate)` applies these rules:

1. Validate and snapshot the complete program.
2. Construct fresh reference state with `Initial`.
3. For a sequential call, invoke the candidate, apply `Step`, and retain only
   reference states whose expected observation matches the actual observation.
4. For a parallel step, launch all candidate calls together, record their
   invocation and completion order, and search the corresponding legal
   reference-transition orderings. A call that completed before another was
   invoked must appear first. Prune observation mismatches and memoize
   equivalent used-call-set and serialized-state prefixes. Retain every
   distinct matching successor state.
5. For a crash, call the candidate's `Crash` and transform every viable
   reference state with `AfterCrash`.
6. For a start, call the candidate's `Start`. Starting does not otherwise
   mutate logical reference state.
7. Return the complete trace, or a counterexample at the first step for which no
   viable reference execution remains.

Crash and start are quiescent barriers. Every preceding call has completed
before a crash begins, and no following call begins before a start completes.
The contract therefore tests durability of acknowledged operations. It does
not yet model ambiguous outcomes from killing a service while a request is in
flight.

The framework snapshots programs, candidate observations, and reference states
through JSON before execution and while branching. Application action, state,
and observation types must therefore round-trip through `encoding/json`. This
prevents a candidate adapter or one explored ordering from mutating values seen
by another reference branch.

The verifier's trace and counterexample are framework-owned diagnostic
artifacts:

```go
type Trace[O any] struct {
    SchemaVersion int            `json:"schema_version"`
    ProgramID     string         `json:"program_id"`
    Steps         []TraceStep[O] `json:"steps"`
}

type TraceStep[O any] struct {
    Step  int            `json:"step"`
    Kind  EventKind      `json:"kind"`
    Calls []CallTrace[O] `json:"calls,omitempty"`
    Error string         `json:"error,omitempty"`
}

type CallTrace[O any] struct {
    ID             string `json:"id"`
    InvokedOrder   int64  `json:"invoked_order"`
    CompletedOrder int64  `json:"completed_order"`
    Observation    *O     `json:"observation,omitempty"`
    Error          string `json:"error,omitempty"`
}

type ProgramCounterexample[A, O any] struct {
    SchemaVersion int        `json:"schema_version"`
    Program       Program[A] `json:"program"`
    Trace         Trace[O]   `json:"trace"`
    Step          int        `json:"step"`
    Reason        string     `json:"reason"`
}
```

Parallel call evidence remains in declaration order. Invocation and completion
orders capture real-time precedence without relying on wall-clock timestamps.
The reference search may reorder overlapping calls, but never places a call
before another call that completed before it was invoked. A `ProgramCounterexample`
contains the first failing step, diagnostic reason, candidate trace, and the
shortest executed program prefix ending at that step. Replay uses this embedded
program rather than regenerating events from a random seed.

## Minimal example

Consider a counter service with `read` and `increment` operations. The
application-owned types and reference model could be:

```go
type Action struct {
    Kind string `json:"kind"`
}

type Observation struct {
    Value int `json:"value"`
}

type CounterReference struct{}

func (CounterReference) Initial() (int, error) { return 0, nil }

func (CounterReference) Step(state int, action Action) (int, Observation, error) {
    switch action.Kind {
    case "read":
        return state, Observation{Value: state}, nil
    case "increment":
        state++
        return state, Observation{Value: state}, nil
    default:
        return state, Observation{}, fmt.Errorf("unknown counter action %q", action.Kind)
    }
}

func (CounterReference) AfterCrash(state int) (int, error) { return state, nil }

func (CounterReference) Equal(want, got Observation) bool { return want == got }
```

The candidate adapter owns the real request mapping:

```go
type CounterCandidate struct {
    BaseURL string
    Client  *http.Client
}

func (c CounterCandidate) Invoke(ctx context.Context, action Action) (Observation, error) {
    var method, path string
    switch action.Kind {
    case "read":
        method, path = http.MethodGet, "/value"
    case "increment":
        method, path = http.MethodPost, "/increment"
    default:
        return Observation{}, fmt.Errorf("unknown counter action %q", action.Kind)
    }
    request, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, nil)
    if err != nil {
        return Observation{}, err
    }
    response, err := c.Client.Do(request)
    if err != nil {
        return Observation{}, err
    }
    defer response.Body.Close()
    if response.StatusCode != http.StatusOK {
        return Observation{}, fmt.Errorf("counter returned HTTP %d", response.StatusCode)
    }
    var observation Observation
    if err := json.NewDecoder(response.Body).Decode(&observation); err != nil {
        return Observation{}, err
    }
    return observation, nil
}
```

A generated or handwritten program can exercise all framework events:

```go
program := accuracy.Program[Action]{
    SchemaVersion: accuracy.ProgramSchemaVersion,
    ID:            "concurrent-durable-increments",
    Steps: []accuracy.Step[Action]{
        {Call: &accuracy.Call[Action]{ID: "initial", Action: Action{Kind: "read"}}},
        {Parallel: &accuracy.Parallel[Action]{Calls: []accuracy.Call[Action]{
            {ID: "increment-a", Action: Action{Kind: "increment"}},
            {ID: "increment-b", Action: Action{Kind: "increment"}},
        }}},
        {Crash: &accuracy.Crash{}},
        {Start: &accuracy.Start{}},
        {Call: &accuracy.Call[Action]{ID: "read-after-restart", Action: Action{Kind: "read"}}},
    },
}

counterCandidate := CounterCandidate{
    BaseURL: "http://127.0.0.1:8080",
    Client:  http.DefaultClient,
}
trace, err := accuracy.VerifyProgram(
    ctx,
    program,
    CounterReference{},
    accuracy.Candidate[Action, Observation]{
        Invoke: counterCandidate.Invoke,
        Crash:  managedCandidate.Crash,
        Start:  managedCandidate.Start,
    },
)
```

Here `counterCandidate` and `managedCandidate` are application wiring values.
The generic verifier establishes that both parallel increments can be explained
by a legal serial order and that the resulting value remains visible after
restart.

## Ownership boundary

The shared package owns:

- program validation, strict decoding, and replay;
- sequential and parallel scheduling;
- linearization search and reference-state branch isolation;
- lifecycle event ordering;
- trace and counterexample construction;
- target sessions, readiness, process containment, property enforcement,
  reporting, and cleanup primitives used by the accuracy runner; and
- strict reusable HTTP and JSON validation helpers.

The task or example owns:

- action, state, and observation types;
- generators and generated input distributions;
- the reference model and observation equivalence;
- protocol-to-action request mappings and response normalization;
- endpoint topology, fixtures, catalogs, and entity relationships; and
- the choice of which event programs constitute adequate qualification.

New application semantics must not be added under `accuracy/`. A task-owned
accuracy application may import this package and register itself from its own
composition command. `accuracyapps/` contains legacy bundled adapters for
existing workloads; it is not required for task-owned extensions.

## Limits

Version 1 intentionally does not provide:

- calls concurrent with crash or start;
- partial-order histories spanning multiple parallel groups;
- cancellation, partitions, clock events, or per-node lifecycle operations;
- nondeterministic reference transitions for a single state and action;
- automatic shrinking of stateful histories; or
- a framework-owned generator policy.

These require additional semantics, not merely more event variants. In
particular, removing an action while shrinking can invalidate later
preconditions, and crashing with requests in flight requires an explicit model
for whether unacknowledged operations may take effect.

Parallel verification uses a sound memoized search, but its worst case remains
factorial when every ordering reaches a distinct state. The framework therefore
rejects groups wider than `MaxParallelCalls` (currently 8). Generators should
keep groups smaller when the reference state distinguishes many orderings.
