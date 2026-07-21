package engine

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/registry"
	"vibesys/microservice-evaluator/transport"
)

type fakeDriver struct {
	delay time.Duration
}

func (d fakeDriver) Protocol() string { return "fake-rpc" }

func (d fakeDriver) Open(context.Context, api.Target) (api.Client, error) {
	return fakeClient{delay: d.delay}, nil
}

type fakeClient struct {
	delay time.Duration
}

func (c fakeClient) Invoke(ctx context.Context, _ api.Invocation) api.ProtocolResult {
	timer := time.NewTimer(c.delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return api.ProtocolResult{ErrorCategory: "timeout", ErrorMessage: ctx.Err().Error()}
	case <-timer.C:
		return api.ProtocolResult{TransportSuccess: true, NativeStatus: "OK", RequestBytes: 2, ResponseBytes: 3}
	}
}

func (fakeClient) Close() error { return nil }

type fakeApplication struct {
	validationDelay time.Duration
}

func (fakeApplication) Name() string { return "fake-app" }
func (fakeApplication) Prepare(context.Context, api.Runtime, api.TrialContext) (any, error) {
	return nil, nil
}
func (fakeApplication) Reset(context.Context, api.Runtime, api.TrialContext) error { return nil }
func (fakeApplication) BuildOperation(operation api.Operation, _ api.Sample, _ any) (api.OperationPlan, error) {
	return api.OperationPlan{Invocations: []api.Invocation{{
		Target: operation.Target, Operation: operation.Name, Payload: "opaque-schema",
	}}}, nil
}
func (a fakeApplication) ValidateOperation(_ api.Operation, _ api.OperationPlan, results []api.ProtocolResult) api.ValidationResult {
	if a.validationDelay > 0 {
		time.Sleep(a.validationDelay)
	}
	if len(results) != 1 {
		return api.ValidationResult{ErrorCategory: "result_count", ErrorMessage: "unexpected result count"}
	}
	result := results[0]
	if !result.TransportSuccess {
		return api.ValidationResult{ErrorCategory: result.ErrorCategory, ErrorMessage: result.ErrorMessage}
	}
	return api.ValidationResult{Success: true}
}
func (fakeApplication) FinishOperation(api.OperationPlan) {}

type noSkipApplication struct{ fakeApplication }

func (noSkipApplication) SupportsSkipPrepare() bool { return false }

func workload(rate float64, duration float64, concurrency int, repetitions int) api.Workload {
	zero := 0.0
	return api.Workload{
		Version: api.WorkloadVersion, Name: "test", Application: "fake-app",
		Load: api.Load{
			Model: "open_loop", Rate: rate, DurationSeconds: duration,
			Concurrency: concurrency, TimeoutSeconds: 1, Repetitions: repetitions,
			MinOfferedRateRatio: 0.95,
		},
		Targets:     []api.Target{{Name: "service", Protocol: "fake-rpc", Address: "fake://service", SessionPolicy: "reuse"}},
		Operations:  []api.Operation{{Name: "read", Target: "service", Weight: 1, Tags: []string{"read"}}},
		Objective:   api.Objective{Name: "p50_ms", Metric: "latency_ms.p50", Direction: "minimize", Unit: "ms", Tags: []string{"read"}},
		Constraints: api.Constraints{MaxErrorRate: &zero},
	}
}

func newTestEngine(t *testing.T, delay time.Duration) *Engine {
	t.Helper()
	registered := registry.New()
	if err := registered.RegisterDriver(fakeDriver{delay: delay}); err != nil {
		t.Fatal(err)
	}
	if err := registered.RegisterApplication("fake-app", func(api.Workload) (api.Application, error) {
		return fakeApplication{}, nil
	}); err != nil {
		t.Fatal(err)
	}
	return New(registered, Options{EngineVersion: "test", WorkloadHash: "hash"})
}

func TestEngineRejectsUnsupportedSkipPrepareBeforeOpeningTargets(t *testing.T) {
	registered := registry.New()
	if err := registered.RegisterApplication("fake-app", func(api.Workload) (api.Application, error) {
		return noSkipApplication{}, nil
	}); err != nil {
		t.Fatal(err)
	}
	_, err := New(registered, Options{SkipPrepare: true}).Run(context.Background(), workload(1, 0.01, 1, 1))
	if err == nil || !strings.Contains(err.Error(), "does not support --skip-prepare") {
		t.Fatalf("unsupported skip prepare error = %v", err)
	}
}

func TestEngineSupportsProtocolThroughDriverContract(t *testing.T) {
	configured := workload(20, 0.11, 2, 3)
	configured.Load.Seed = 42
	configured.Load.FixtureSeed = 99
	run, err := newTestEngine(t, time.Millisecond).Run(context.Background(), configured)
	if err != nil {
		t.Fatal(err)
	}
	if !run.Summary.Valid || run.Summary.PrimaryValue == nil {
		t.Fatalf("unexpected summary: %+v", run.Summary)
	}
	if run.Summary.Seed != "42" || run.Summary.FixtureSeed != "99" {
		t.Fatalf("independent seeds not recorded: %+v", run.Summary)
	}
	if len(run.Summary.Trials) != 3 || len(run.Observations) != 9 {
		t.Fatalf("unexpected result counts: trials=%d observations=%d", len(run.Summary.Trials), len(run.Observations))
	}
	if run.Summary.Aggregate.MAD == nil || len(run.Summary.Aggregate.CI95) != 2 {
		t.Fatalf("missing robust aggregate: %+v", run.Summary.Aggregate)
	}
	for _, observation := range run.Observations {
		if observation.Protocol != "fake-rpc" || observation.NativeStatus != "OK" {
			t.Fatalf("protocol details not preserved: %+v", observation)
		}
	}
}

func TestEngineExposesQueueDelayAndInvalidatesClientSaturation(t *testing.T) {
	run, err := newTestEngine(t, 10*time.Millisecond).Run(context.Background(), workload(500, 0.03, 1, 1))
	if err != nil {
		t.Fatal(err)
	}
	if run.Summary.Valid {
		t.Fatalf("expected invalid saturated result: %+v", run.Summary)
	}
	trial := run.Summary.Trials[0]
	if trial.Generator.Sustained {
		t.Fatalf("generator unexpectedly sustained load: %+v", trial.Generator)
	}
	if trial.QueueWaitMS.P99 == nil || *trial.QueueWaitMS.P99 < 5 {
		t.Fatalf("queue wait was not captured: %+v", trial.QueueWaitMS)
	}
	if trial.PrimaryValue != nil || run.Summary.PrimaryValue != nil {
		t.Fatal("invalid result exposed a trusted primary value")
	}
	if len(trial.InvalidReasons) == 0 {
		t.Fatal("invalid result did not explain why it failed")
	}
}

func TestEngineClosedLoopMeasuresSaturationThroughput(t *testing.T) {
	configured := workload(0, 0.05, 2, 1)
	configured.Load.Model = "closed_loop"
	configured.Objective = api.Objective{
		Name: "operations_per_second", Metric: "operations_per_second", Direction: "maximize", Unit: "operations/s",
	}
	run, err := newTestEngine(t, time.Millisecond).Run(context.Background(), configured)
	if err != nil {
		t.Fatal(err)
	}
	trial := run.Summary.Trials[0]
	if !run.Summary.Valid || run.Summary.PrimaryValue == nil || *run.Summary.PrimaryValue <= 0 {
		t.Fatalf("closed-loop throughput was not measured: %+v", run.Summary)
	}
	if trial.Generator.TargetRate != 0 || !trial.Generator.Sustained || trial.Generator.SubmittedOperations == 0 {
		t.Fatalf("unexpected closed-loop generator report: %+v", trial.Generator)
	}
}

func TestClosedLoopThroughputIncludesSemanticValidationTail(t *testing.T) {
	registered := registry.New()
	if err := registered.RegisterDriver(fakeDriver{}); err != nil {
		t.Fatal(err)
	}
	if err := registered.RegisterApplication("fake-app", func(api.Workload) (api.Application, error) {
		return fakeApplication{validationDelay: 100 * time.Millisecond}, nil
	}); err != nil {
		t.Fatal(err)
	}
	configured := workload(0, 0.01, 1, 1)
	configured.Load.Model = "closed_loop"
	configured.Objective = api.Objective{
		Name: "operations_per_second", Metric: "operations_per_second", Direction: "maximize", Unit: "operations/s",
	}
	run, err := New(registered, Options{EngineVersion: "test"}).Run(context.Background(), configured)
	if err != nil {
		t.Fatal(err)
	}
	trial := run.Summary.Trials[0]
	if trial.ElapsedSeconds < 0.09 {
		t.Fatalf("validation tail was omitted from elapsed time: %+v", trial)
	}
	if run.Summary.PrimaryValue == nil || *run.Summary.PrimaryValue > 20 {
		t.Fatalf("validation tail inflated throughput: %+v", run.Summary)
	}
	if got := run.Observations[0].ValidationTimeMS; got < 90 {
		t.Fatalf("validation duration was not retained: %.2fms", got)
	}
}

func TestTrialRequiresConfiguredOperationCoverage(t *testing.T) {
	configured := workload(1, 1, 1, 1)
	configured.Operations = append(configured.Operations, api.Operation{
		Name: "uncovered", Target: "service", Weight: 1,
	})
	configured.Constraints.MinOperationsPerType = 1
	now := time.Now()
	observation := api.Observation{
		Operation: "read", ScheduledAt: now, CompletedAt: now.Add(time.Millisecond),
		ValidatedAt: now.Add(time.Millisecond), ApplicationSuccess: true,
	}
	observation.PopulateDurations()
	trial := summarizeTrial(0, []api.Observation{observation}, GeneratorReport{Sustained: true}, configured)
	if trial.Valid || trial.PrimaryValue != nil {
		t.Fatalf("missing operation coverage unexpectedly passed: %+v", trial)
	}
	if len(trial.InvalidReasons) == 0 || !strings.Contains(strings.Join(trial.InvalidReasons, " "), "uncovered") {
		t.Fatalf("missing operation was not identified: %+v", trial.InvalidReasons)
	}
}

func TestRegistryRejectsUnregisteredProtocol(t *testing.T) {
	registered := registry.New()
	if err := registered.RegisterApplication("fake-app", func(api.Workload) (api.Application, error) {
		return fakeApplication{}, nil
	}); err != nil {
		t.Fatal(err)
	}
	_, err := New(registered, Options{}).Run(context.Background(), workload(1, 1, 1, 1))
	if err == nil || err.Error() == "" {
		t.Fatal("expected unsupported protocol error")
	}
	if got := fmt.Sprint(err); got == "" {
		t.Fatal("empty error")
	}
}

type multiStepApplication struct {
	finished int
}

func (*multiStepApplication) Name() string { return "multi" }
func (*multiStepApplication) Prepare(context.Context, api.Runtime, api.TrialContext) (any, error) {
	return nil, nil
}
func (*multiStepApplication) Reset(context.Context, api.Runtime, api.TrialContext) error {
	return nil
}
func (*multiStepApplication) BuildOperation(operation api.Operation, _ api.Sample, _ any) (api.OperationPlan, error) {
	return api.OperationPlan{Invocations: []api.Invocation{
		{Target: operation.Target, Operation: operation.Name},
		{Target: operation.Target, Operation: operation.Name},
	}}, nil
}
func (*multiStepApplication) ValidateOperation(_ api.Operation, _ api.OperationPlan, results []api.ProtocolResult) api.ValidationResult {
	return api.ValidationResult{Success: len(results) == 2}
}
func (a *multiStepApplication) FinishOperation(api.OperationPlan) { a.finished++ }

func TestExecuteRequestAccountsForEveryInvocationInLogicalOperation(t *testing.T) {
	application := &multiStepApplication{}
	registered := registry.New()
	if err := registered.RegisterDriver(fakeDriver{delay: time.Millisecond}); err != nil {
		t.Fatal(err)
	}
	runtime, err := transport.Open(context.Background(), registered, []api.Target{{
		Name: "service", Protocol: "fake-rpc", Address: "fake://service",
	}})
	if err != nil {
		t.Fatal(err)
	}
	defer runtime.Close()
	observation := executeRequest(
		context.Background(), api.PhaseMeasurement, 0,
		api.Load{TimeoutSeconds: 1},
		scheduledSample{
			operation: api.Operation{Name: "transaction", Target: "service"},
			scheduled: time.Now(),
		},
		application, runtime, nil,
	)
	if !observation.ApplicationSuccess {
		t.Fatalf("logical operation failed: %+v", observation)
	}
	if observation.InvocationCount != 2 || observation.RequestBytes != 4 || observation.ResponseBytes != 6 {
		t.Fatalf("invocations were not aggregated: %+v", observation)
	}
	if len(observation.NativeStatuses) != 2 || application.finished != 1 {
		t.Fatalf("statuses or cleanup were not preserved: observation=%+v finished=%d", observation, application.finished)
	}
}
