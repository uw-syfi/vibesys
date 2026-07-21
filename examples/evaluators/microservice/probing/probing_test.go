package probing

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"vibesys/microservice-evaluator/api"
)

type runtimeFunc func(context.Context, api.Invocation) api.ProtocolResult

func (function runtimeFunc) Invoke(ctx context.Context, invocation api.Invocation) api.ProtocolResult {
	return function(ctx, invocation)
}

func testOptions() Options {
	return Options{PhaseTimeout: 50 * time.Millisecond, ProbeTimeout: 10 * time.Millisecond, Interval: time.Millisecond}
}

func TestRunTransportGatesPermissiveValidator(t *testing.T) {
	validated := false
	probe := api.ReadinessProbe{
		Name: "permissive", Invocation: api.Invocation{Target: "service"},
		Validate: func(api.ProtocolResult) error { validated = true; return nil },
	}
	err := Run(context.Background(), runtimeFunc(func(context.Context, api.Invocation) api.ProtocolResult {
		return api.ProtocolResult{ErrorCategory: "transport", ErrorMessage: "offline"}
	}), []api.ReadinessProbe{probe}, testOptions())
	if err == nil || !strings.Contains(err.Error(), "transport failed") || validated {
		t.Fatalf("err=%v validated=%v", err, validated)
	}
}

func TestRunUsesOneAggregateDeadline(t *testing.T) {
	probe := func(name string) api.ReadinessProbe {
		return api.ReadinessProbe{
			Name: name, Invocation: api.Invocation{Target: "service"},
			Validate: func(api.ProtocolResult) error { return nil },
		}
	}
	options := testOptions()
	options.PhaseTimeout = 25 * time.Millisecond
	options.ProbeTimeout = 100 * time.Millisecond
	started := time.Now()
	err := Run(context.Background(), runtimeFunc(func(ctx context.Context, _ api.Invocation) api.ProtocolResult {
		select {
		case <-ctx.Done():
			return api.ProtocolResult{ErrorCategory: "timeout", ErrorMessage: ctx.Err().Error()}
		case <-time.After(20 * time.Millisecond):
			return api.ProtocolResult{TransportSuccess: true}
		}
	}), []api.ReadinessProbe{probe("one"), probe("two")}, options)
	if err == nil || time.Since(started) > 60*time.Millisecond {
		t.Fatalf("err=%v elapsed=%s", err, time.Since(started))
	}
}

func TestValidateRequiresCompleteKnownTargetCoverage(t *testing.T) {
	targets := []api.Target{{Name: "one"}, {Name: "two"}}
	probe := func(name, target string) api.ReadinessProbe {
		return api.ReadinessProbe{
			Name: name, Invocation: api.Invocation{Target: target},
			Validate: func(api.ProtocolResult) error { return errors.New("unused") },
		}
	}
	if err := Validate([]api.ReadinessProbe{probe("one", "one")}, targets, true); err == nil {
		t.Fatal("missing target coverage was accepted")
	}
	if err := Validate([]api.ReadinessProbe{
		probe("one", "one"), probe("unknown", "unknown"),
	}, targets, true); err == nil {
		t.Fatal("unknown target was accepted")
	}
	if err := Validate([]api.ReadinessProbe{
		probe("one", "one"), probe("two", "two"),
	}, targets, true); err != nil {
		t.Fatal(err)
	}
}
