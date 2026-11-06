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
	err := Run(context.Background(), runtimeFunc(func(ctx context.Context, _ api.Invocation) api.ProtocolResult {
		select {
		case <-ctx.Done():
			return api.ProtocolResult{ErrorCategory: "timeout", ErrorMessage: ctx.Err().Error()}
		case <-time.After(20 * time.Millisecond):
			return api.ProtocolResult{TransportSuccess: true}
		}
	}), []api.ReadinessProbe{probe("one"), probe("two")}, options)
	// The verdict is the error alone: elapsed time is not asserted, because a loaded machine
	// stretches it.
	if err == nil {
		t.Fatal("two 20ms probes ran to completion under a 25ms aggregate deadline")
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

func stopProbe(name string) api.ReadinessProbe {
	return api.ReadinessProbe{
		Name: name, Invocation: api.Invocation{Target: "service"},
		Validate: func(api.ProtocolResult) error { return nil },
	}
}

func TestWaitStoppedReturnsWhenEndpointsGoQuiet(t *testing.T) {
	answers := make(chan struct{}, 1)
	answers <- struct{}{}
	err := WaitStopped(context.Background(), runtimeFunc(
		func(context.Context, api.Invocation) api.ProtocolResult {
			select {
			case <-answers:
				return api.ProtocolResult{TransportSuccess: true}
			default:
				return api.ProtocolResult{ErrorCategory: "transport", ErrorMessage: "refused"}
			}
		},
	), []api.ReadinessProbe{stopProbe("one")}, testOptions())
	if err != nil {
		t.Fatal(err)
	}
}

// A candidate observed still answering must be named as reachable no matter
// where the phase deadline lands. The first sweep answers immediately and every
// later probe blocks until the deadline, so the deadline expires mid-sweep and
// the sweep it interrupts proves nothing. That path used to report a bare
// timeout, which is what made this flaky under load.
func TestWaitStoppedNamesReachableEndpointsWhenTheDeadlineLandsMidSweep(t *testing.T) {
	options := testOptions()
	options.PhaseTimeout = 30 * time.Millisecond
	options.ProbeTimeout = 50 * time.Millisecond
	// Probes are invoked sequentially from the calling goroutine, so a plain
	// counter is enough to make the first sweep the only conclusive one.
	sweeps := 0
	err := WaitStopped(context.Background(), runtimeFunc(
		func(ctx context.Context, _ api.Invocation) api.ProtocolResult {
			sweeps++
			if sweeps == 1 {
				return api.ProtocolResult{TransportSuccess: true}
			}
			<-ctx.Done()
			return api.ProtocolResult{ErrorCategory: "transport", ErrorMessage: ctx.Err().Error()}
		},
	), []api.ReadinessProbe{stopProbe("one")}, options)
	if err == nil || !strings.Contains(err.Error(), "remained reachable") {
		t.Fatalf("err=%v", err)
	}
	if !strings.Contains(err.Error(), "one") {
		t.Fatalf("reachable endpoints not named: %v", err)
	}
}

// Nothing was ever observed serving, so there is no reachable endpoint to name
// and the timeout is the only honest report.
func TestWaitStoppedReportsATimeoutWhenNoEndpointWasEverReachable(t *testing.T) {
	options := testOptions()
	options.PhaseTimeout = 20 * time.Millisecond
	options.ProbeTimeout = 50 * time.Millisecond
	err := WaitStopped(context.Background(), runtimeFunc(
		func(ctx context.Context, _ api.Invocation) api.ProtocolResult {
			<-ctx.Done()
			return api.ProtocolResult{ErrorCategory: "transport", ErrorMessage: ctx.Err().Error()}
		},
	), []api.ReadinessProbe{stopProbe("one")}, options)
	if err == nil || !strings.Contains(err.Error(), "did not stop within") {
		t.Fatalf("err=%v", err)
	}
}
