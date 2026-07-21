// Package probing executes application-declared readiness and protocol probes.
// It is shared by benchmark and accuracy modes so both use identical transport,
// timeout, target-coverage, and validation behavior.
package probing

import (
	"context"
	"fmt"
	"time"

	"vibesys/microservice-evaluator/api"
)

type Options struct {
	PhaseTimeout time.Duration
	ProbeTimeout time.Duration
	Interval     time.Duration
}

func (o Options) validate() error {
	if o.PhaseTimeout <= 0 {
		return fmt.Errorf("probe phase timeout must be positive")
	}
	if o.ProbeTimeout <= 0 {
		return fmt.Errorf("individual probe timeout must be positive")
	}
	if o.Interval <= 0 {
		return fmt.Errorf("probe interval must be positive")
	}
	return nil
}

// Validate rejects incomplete or malformed probe plans before traffic starts.
// Readiness plans should require target coverage; protocol-check plans need not.
func Validate(probes []api.ReadinessProbe, targets []api.Target, requireCoverage bool) error {
	if len(probes) == 0 {
		return fmt.Errorf("probe plan is empty")
	}
	targetNames := make(map[string]struct{}, len(targets))
	for _, target := range targets {
		targetNames[target.Name] = struct{}{}
	}
	covered := make(map[string]struct{}, len(targetNames))
	seen := make(map[string]struct{}, len(probes))
	for index, probe := range probes {
		if probe.Name == "" {
			return fmt.Errorf("probe %d has an empty name", index)
		}
		if _, duplicate := seen[probe.Name]; duplicate {
			return fmt.Errorf("duplicate probe name %q", probe.Name)
		}
		seen[probe.Name] = struct{}{}
		if probe.Validate == nil {
			return fmt.Errorf("probe %q has no validator", probe.Name)
		}
		if probe.Invocation.Target == "" {
			return fmt.Errorf("probe %q has an empty target", probe.Name)
		}
		if _, exists := targetNames[probe.Invocation.Target]; !exists {
			return fmt.Errorf(
				"probe %q references unknown target %q",
				probe.Name,
				probe.Invocation.Target,
			)
		}
		covered[probe.Invocation.Target] = struct{}{}
	}
	if requireCoverage {
		for target := range targetNames {
			if _, exists := covered[target]; !exists {
				return fmt.Errorf("probe plan does not cover target %q", target)
			}
		}
	}
	return nil
}

func WaitReady(
	ctx context.Context,
	runtime api.Runtime,
	probes []api.ReadinessProbe,
	options Options,
) error {
	if err := options.validate(); err != nil {
		return err
	}
	phase, cancel := context.WithTimeout(ctx, options.PhaseTimeout)
	defer cancel()
	last := "not attempted"
	for {
		if err := phase.Err(); err != nil {
			return fmt.Errorf("candidate did not become ready within %s: %s", options.PhaseTimeout, last)
		}
		allReady := true
		for _, probe := range probes {
			result := invoke(phase, runtime, probe, options.ProbeTimeout)
			if !result.TransportSuccess {
				allReady = false
				last = fmt.Sprintf(
					"%s: transport failed (%s): %s",
					probe.Name,
					result.ErrorCategory,
					result.ErrorMessage,
				)
				break
			}
			if err := probe.Validate(result); err != nil {
				allReady = false
				last = fmt.Sprintf("%s: %v", probe.Name, err)
				break
			}
		}
		if allReady {
			if err := phase.Err(); err != nil {
				return fmt.Errorf("candidate did not become ready within %s: %s", options.PhaseTimeout, last)
			}
			return nil
		}
		if err := sleep(phase, options.Interval); err != nil {
			return fmt.Errorf("candidate did not become ready within %s: %s", options.PhaseTimeout, last)
		}
	}
}

func WaitStopped(
	ctx context.Context,
	runtime api.Runtime,
	probes []api.ReadinessProbe,
	options Options,
) error {
	if err := options.validate(); err != nil {
		return err
	}
	phase, cancel := context.WithTimeout(ctx, options.PhaseTimeout)
	defer cancel()
	for {
		if err := phase.Err(); err != nil {
			return fmt.Errorf("candidate did not stop within %s", options.PhaseTimeout)
		}
		serving := make([]string, 0)
		for _, probe := range probes {
			result := invoke(phase, runtime, probe, options.ProbeTimeout)
			if result.TransportSuccess {
				serving = append(serving, probe.Name)
			}
		}
		if len(serving) == 0 {
			if err := phase.Err(); err != nil {
				return fmt.Errorf("candidate did not stop within %s", options.PhaseTimeout)
			}
			return nil
		}
		if err := sleep(phase, options.Interval); err != nil {
			return fmt.Errorf("candidate endpoints remained reachable after stop: %v", serving)
		}
	}
}

// Run executes every protocol probe exactly once, sequentially, under one
// aggregate deadline. Sequential execution is required for connection-reuse
// probes and makes the preflight traffic identical between evaluator modes.
func Run(
	ctx context.Context,
	runtime api.Runtime,
	probes []api.ReadinessProbe,
	options Options,
) error {
	if err := options.validate(); err != nil {
		return err
	}
	phase, cancel := context.WithTimeout(ctx, options.PhaseTimeout)
	defer cancel()
	for _, probe := range probes {
		if err := phase.Err(); err != nil {
			return fmt.Errorf("preflight exceeded %s before %q", options.PhaseTimeout, probe.Name)
		}
		result := invoke(phase, runtime, probe, options.ProbeTimeout)
		if !result.TransportSuccess {
			return fmt.Errorf(
				"preflight %q transport failed (%s): %s",
				probe.Name,
				result.ErrorCategory,
				result.ErrorMessage,
			)
		}
		if err := probe.Validate(result); err != nil {
			return fmt.Errorf("preflight %q: %w", probe.Name, err)
		}
	}
	return nil
}

func invoke(
	ctx context.Context,
	runtime api.Runtime,
	probe api.ReadinessProbe,
	timeout time.Duration,
) api.ProtocolResult {
	request, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return runtime.Invoke(request, probe.Invocation)
}

func sleep(ctx context.Context, duration time.Duration) error {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
