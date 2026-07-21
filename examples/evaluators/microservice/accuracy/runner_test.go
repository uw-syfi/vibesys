package accuracy

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/registry"
)

type runnerDriver struct{ serving *atomic.Bool }

func (runnerDriver) Protocol() string { return "test" }
func (d runnerDriver) Open(context.Context, api.Target) (api.Client, error) {
	return runnerClient{serving: d.serving}, nil
}

type runnerClient struct{ serving *atomic.Bool }

func (c runnerClient) Invoke(context.Context, api.Invocation) api.ProtocolResult {
	if !c.serving.Load() {
		return api.ProtocolResult{ErrorCategory: "transport", ErrorMessage: "stopped"}
	}
	return api.ProtocolResult{TransportSuccess: true, NativeStatus: "ready"}
}
func (runnerClient) Close() error { return nil }

type runnerApplication struct {
	restart    bool
	pass       bool
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
func (runnerApplication) ReadinessProbes() []api.ReadinessProbe {
	return []api.ReadinessProbe{{
		Name: "service", Invocation: api.Invocation{Target: "service"},
		Validate: func(result api.ProtocolResult) error {
			if !result.TransportSuccess {
				return errors.New("not ready")
			}
			return nil
		},
	}}
}
func (a runnerApplication) Check(
	ctx context.Context,
	_ api.Runtime,
	check api.AccuracyContext,
	recorder api.AccuracyRecorder,
) error {
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
