package engine

import (
	"context"
	"fmt"
	"testing"
	"time"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/registry"
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
		return api.ProtocolResult{TransportSuccess: true, NativeStatus: "OK"}
	}
}

func (fakeClient) Close() error { return nil }

type fakeApplication struct{}

func (fakeApplication) Name() string { return "fake-app" }
func (fakeApplication) Prepare(context.Context, api.Runtime, api.TrialContext) (any, error) {
	return nil, nil
}
func (fakeApplication) Reset(context.Context, api.Runtime, api.TrialContext) error { return nil }
func (fakeApplication) BuildInvocation(operation api.Operation, _ api.Sample, _ any) (api.Invocation, error) {
	return api.Invocation{Target: operation.Target, Operation: operation.Name, Payload: "opaque-schema"}, nil
}
func (fakeApplication) Validate(_ api.Operation, result api.ProtocolResult) api.ValidationResult {
	if !result.TransportSuccess {
		return api.ValidationResult{ErrorCategory: result.ErrorCategory, ErrorMessage: result.ErrorMessage}
	}
	return api.ValidationResult{Success: true}
}

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

func TestEngineSupportsProtocolThroughDriverContract(t *testing.T) {
	run, err := newTestEngine(t, time.Millisecond).Run(context.Background(), workload(20, 0.11, 2, 3))
	if err != nil {
		t.Fatal(err)
	}
	if !run.Summary.Valid || run.Summary.PrimaryValue == nil {
		t.Fatalf("unexpected summary: %+v", run.Summary)
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
