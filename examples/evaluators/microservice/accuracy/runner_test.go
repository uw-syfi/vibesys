package accuracy

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/registry"
)

type runnerDriver struct {
	serving *atomic.Bool
	delay   time.Duration
}

func (runnerDriver) Protocol() string { return "test" }
func (d runnerDriver) Open(context.Context, api.Target) (api.Client, error) {
	return runnerClient{serving: d.serving, delay: d.delay}, nil
}

type runnerClient struct {
	serving *atomic.Bool
	delay   time.Duration
}

func (c runnerClient) Invoke(ctx context.Context, _ api.Invocation) api.ProtocolResult {
	if c.delay > 0 {
		select {
		case <-ctx.Done():
			return api.ProtocolResult{ErrorCategory: "transport", ErrorMessage: ctx.Err().Error()}
		case <-time.After(c.delay):
		}
	}
	if !c.serving.Load() {
		return api.ProtocolResult{ErrorCategory: "transport", ErrorMessage: "stopped"}
	}
	return api.ProtocolResult{TransportSuccess: true, NativeStatus: "ready"}
}
func (runnerClient) Close() error { return nil }

type runnerApplication struct {
	restart    bool
	pass       bool
	probeCount int
	permissive bool
	panicCheck bool
	properties []api.AccuracyProperty
}

func (runnerApplication) Name() string { return "runner-test" }
func (a runnerApplication) Properties() []api.AccuracyProperty {
	if a.properties != nil {
		return a.properties
	}
	return []api.AccuracyProperty{
		{Name: "required", Required: true},
		{Name: "restart", Required: false},
	}
}
func (a runnerApplication) ReadinessProbes() []api.ReadinessProbe {
	count := a.probeCount
	if count == 0 {
		count = 1
	}
	probes := make([]api.ReadinessProbe, 0, count)
	for index := 0; index < count; index++ {
		probes = append(probes, api.ReadinessProbe{
			Name:       fmt.Sprintf("service-%d", index),
			Invocation: api.Invocation{Target: "service"},
			Validate: func(result api.ProtocolResult) error {
				if a.permissive {
					return nil
				}
				if !result.TransportSuccess {
					return errors.New("not ready")
				}
				return nil
			},
		})
	}
	return probes
}
func (a runnerApplication) Check(
	ctx context.Context,
	_ api.Runtime,
	check api.AccuracyContext,
	recorder api.AccuracyRecorder,
) error {
	if a.panicCheck {
		panic("injected adapter panic")
	}
	recorder.AddChecks(1)
	if a.pass {
		if err := recorder.Pass("required"); err != nil {
			return err
		}
	}
	if a.restart {
		if check.Restart == nil {
			return errors.New("restart missing")
		}
		if err := check.Restart(ctx); err != nil {
			return err
		}
		recorder.AddChecks(1)
		return recorder.Pass("restart")
	}
	return nil
}

type runnerLifecycle struct {
	serving *atomic.Bool
	noOp    bool
}

func (l runnerLifecycle) Prepare(context.Context) error { l.serving.Store(true); return nil }
func (l runnerLifecycle) Stop(context.Context, bool) error {
	if !l.noOp {
		l.serving.Store(false)
	}
	return nil
}
func (l runnerLifecycle) Start(context.Context) error { l.serving.Store(true); return nil }
func (l runnerLifecycle) Close(context.Context) error { l.serving.Store(false); return nil }

func runnerWorkload() api.Workload {
	return api.Workload{
		Application: "runner-test",
		Targets: []api.Target{{
			Name: "service", Protocol: "test", Address: "test://service",
		}},
	}
}

func runTestAccuracy(
	t *testing.T,
	application runnerApplication,
	lifecycle CandidateLifecycle,
	serving *atomic.Bool,
) Result {
	t.Helper()
	registered := registry.New()
	if err := registered.RegisterDriver(runnerDriver{serving: serving}); err != nil {
		t.Fatal(err)
	}
	if err := registered.RegisterAccuracyApplication(
		"runner-test",
		func(api.Workload) (api.AccuracyApplication, error) { return application, nil },
	); err != nil {
		t.Fatal(err)
	}
	runner, err := NewRunner(registered, Options{
		Seed: 7, CasesMin: 1, CasesMax: 1,
		StartupTimeout: 75 * time.Millisecond,
		ProbeTimeout:   10 * time.Millisecond,
		ProbeInterval:  5 * time.Millisecond,
		Lifecycle:      lifecycle,
	})
	if err != nil {
		t.Fatal(err)
	}
	return runner.Run(context.Background(), runnerWorkload())
}

func TestRunnerRejectsUnpassedRequiredProperty(t *testing.T) {
	serving := &atomic.Bool{}
	serving.Store(true)
	result := runTestAccuracy(t, runnerApplication{}, nil, serving)
	if result.Valid || !strings.Contains(result.Error, "required accuracy properties") {
		t.Fatalf("result=%+v", result)
	}
}

func TestRunnerConvertsAdapterPanicIntoInvalidResult(t *testing.T) {
	serving := &atomic.Bool{}
	serving.Store(true)
	result := runTestAccuracy(t, runnerApplication{panicCheck: true}, nil, serving)
	if result.Valid || !strings.Contains(result.Error, "injected adapter panic") {
		t.Fatalf("result=%+v", result)
	}
}

func TestRunnerRejectsApplicationWithoutRequiredProperties(t *testing.T) {
	serving := &atomic.Bool{}
	serving.Store(true)
	result := runTestAccuracy(t, runnerApplication{
		properties: []api.AccuracyProperty{{Name: "optional", Required: false}},
	}, nil, serving)
	if result.Valid || !strings.Contains(result.Error, "no required properties") {
		t.Fatalf("result=%+v", result)
	}
}

func TestRecorderRequiresFreshEvidenceForEachPropertyGroup(t *testing.T) {
	recorder, err := newRecorder([]api.AccuracyProperty{
		{Name: "one", Required: true}, {Name: "two", Required: true},
	})
	if err != nil {
		t.Fatal(err)
	}
	recorder.AddChecks(1)
	if err := recorder.Pass("one"); err != nil {
		t.Fatal(err)
	}
	if err := recorder.Pass("two"); err == nil || !strings.Contains(err.Error(), "no newly recorded checks") {
		t.Fatalf("unsubstantiated property error=%v", err)
	}
	recorder.AddChecks(1)
	if err := recorder.Pass("two"); err != nil {
		t.Fatal(err)
	}
}

func TestRunnerLeavesUnavailableOptionalPropertyFalse(t *testing.T) {
	serving := &atomic.Bool{}
	serving.Store(true)
	result := runTestAccuracy(t, runnerApplication{pass: true}, nil, serving)
	if !result.Valid || result.Properties["restart"] {
		t.Fatalf("result=%+v", result)
	}
}

func TestRunnerProvesStopBeforeRestart(t *testing.T) {
	serving := &atomic.Bool{}
	result := runTestAccuracy(
		t,
		runnerApplication{pass: true, restart: true},
		runnerLifecycle{serving: serving},
		serving,
	)
	if !result.Valid || !result.Properties["restart"] {
		t.Fatalf("result=%+v", result)
	}
}

func TestRunnerRejectsNoOpStop(t *testing.T) {
	serving := &atomic.Bool{}
	result := runTestAccuracy(
		t,
		runnerApplication{pass: true, restart: true},
		runnerLifecycle{serving: serving, noOp: true},
		serving,
	)
	if result.Valid || !strings.Contains(result.Error, "remained reachable") {
		t.Fatalf("result=%+v", result)
	}
}

func TestReadinessUsesAggregateStartupDeadline(t *testing.T) {
	serving := &atomic.Bool{}
	serving.Store(true)
	registered := registry.New()
	if err := registered.RegisterDriver(runnerDriver{
		serving: serving, delay: 30 * time.Millisecond,
	}); err != nil {
		t.Fatal(err)
	}
	application := runnerApplication{pass: true, probeCount: 2}
	if err := registered.RegisterAccuracyApplication(
		"runner-test",
		func(api.Workload) (api.AccuracyApplication, error) { return application, nil },
	); err != nil {
		t.Fatal(err)
	}
	runner, err := NewRunner(registered, Options{
		Seed: 7, CasesMin: 1, CasesMax: 1,
		StartupTimeout: 40 * time.Millisecond,
		ProbeTimeout:   100 * time.Millisecond,
		ProbeInterval:  time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	result := runner.Run(context.Background(), runnerWorkload())
	elapsed := time.Since(started)
	if result.Valid || !strings.Contains(result.Error, "within 40ms") {
		t.Fatalf("result=%+v", result)
	}
	if elapsed > 90*time.Millisecond {
		t.Fatalf("aggregate readiness deadline took %s", elapsed)
	}
}

func TestReadinessRejectsTransportFailureBeforePermissiveValidator(t *testing.T) {
	serving := &atomic.Bool{}
	result := runTestAccuracy(
		t, runnerApplication{pass: true, permissive: true}, nil, serving,
	)
	if result.Valid || !strings.Contains(result.Error, "transport failed") {
		t.Fatalf("result=%+v", result)
	}
}

func TestReadinessProbeDeclarationsCoverKnownTargets(t *testing.T) {
	targets := []api.Target{{Name: "one"}, {Name: "two"}}
	probe := func(name, target string) api.ReadinessProbe {
		return api.ReadinessProbe{
			Name: name, Invocation: api.Invocation{Target: target},
			Validate: func(api.ProtocolResult) error { return nil },
		}
	}
	if err := validateProbes([]api.ReadinessProbe{probe("one", "one")}, targets); err == nil ||
		!strings.Contains(err.Error(), "do not cover") {
		t.Fatalf("missing-target error=%v", err)
	}
	if err := validateProbes([]api.ReadinessProbe{
		probe("one", "one"), probe("unknown", "unknown"),
	}, targets); err == nil || !strings.Contains(err.Error(), "unknown target") {
		t.Fatalf("unknown-target error=%v", err)
	}
	if err := validateProbes([]api.ReadinessProbe{
		probe("one", "one"), probe("two", "two"),
	}, targets); err != nil {
		t.Fatal(err)
	}
}
