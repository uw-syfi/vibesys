package accuracy

import (
	"context"
	"crypto/sha256"
	"fmt"
	"math/rand"
	"strconv"
	"time"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/registry"
	"vibesys/microservice-evaluator/transport"
)

const ResultSchemaVersion = 1

type CandidateLifecycle interface {
	Prepare(context.Context) error
	Stop(context.Context, bool) error
	Start(context.Context) error
	Close(context.Context) error
}

type Options struct {
	EngineVersion  string
	Seed           int64
	CasesMin       int
	CasesMax       int
	StartupTimeout time.Duration
	ProbeTimeout   time.Duration
	ProbeInterval  time.Duration
	Lifecycle      CandidateLifecycle
}

type Result struct {
	SchemaVersion int             `json:"schema_version"`
	EngineVersion string          `json:"engine_version"`
	Application   string          `json:"application"`
	Valid         bool            `json:"valid"`
	Checks        int             `json:"checks"`
	RandomCases   int             `json:"random_cases"`
	SeedSHA256    string          `json:"seed_sha256"`
	Properties    map[string]bool `json:"properties"`
	Error         string          `json:"error,omitempty"`
}

type Runner struct {
	registry *registry.Registry
	options  Options
}

func NewRunner(registry *registry.Registry, options Options) (*Runner, error) {
	if registry == nil {
		return nil, fmt.Errorf("accuracy runner registry is nil")
	}
	if options.EngineVersion == "" {
		options.EngineVersion = "dev"
	}
	if options.CasesMin < 1 || options.CasesMax < options.CasesMin {
		return nil, fmt.Errorf(
			"accuracy case bounds must satisfy 1 <= min <= max, got %d..%d",
			options.CasesMin,
			options.CasesMax,
		)
	}
	if options.StartupTimeout <= 0 {
		return nil, fmt.Errorf("accuracy startup timeout must be positive")
	}
	if options.ProbeTimeout <= 0 {
		return nil, fmt.Errorf("accuracy probe timeout must be positive")
	}
	if options.ProbeInterval <= 0 {
		return nil, fmt.Errorf("accuracy probe interval must be positive")
	}
	return &Runner{registry: registry, options: options}, nil
}

func (r *Runner) Run(ctx context.Context, workload api.Workload) (result Result) {
	result = Result{
		SchemaVersion: ResultSchemaVersion,
		EngineVersion: r.options.EngineVersion,
		Application:   workload.Application,
		SeedSHA256:    seedHash(r.options.Seed),
		Properties:    make(map[string]bool),
	}
	application, err := r.registry.AccuracyApplication(workload)
	if err != nil {
		result.Error = err.Error()
		return result
	}
	result.Application = application.Name()
	recorder, err := newRecorder(application.Properties())
	if err != nil {
		result.Error = err.Error()
		return result
	}
	result.Checks, result.Properties = recorder.snapshot()
	probes := application.ReadinessProbes()
	if err := validateProbes(probes); err != nil {
		result.Error = err.Error()
		return result
	}
	runtime, err := transport.Open(ctx, r.registry, workload.Targets)
	if err != nil {
		result.Error = err.Error()
		return result
	}
	defer func() {
		if closeErr := runtime.Close(); closeErr != nil && result.Error == "" {
			result.Valid = false
			result.Error = closeErr.Error()
		}
	}()

	if r.options.Lifecycle != nil {
		if err := r.options.Lifecycle.Prepare(ctx); err != nil {
			result.Error = fmt.Sprintf("prepare managed candidate: %v", err)
			return result
		}
		defer func() {
			if closeErr := r.options.Lifecycle.Close(context.WithoutCancel(ctx)); closeErr != nil && result.Error == "" {
				result.Valid = false
				result.Error = fmt.Sprintf("close managed candidate: %v", closeErr)
			}
		}()
	}
	if err := r.waitReady(ctx, runtime, probes); err != nil {
		result.Error = err.Error()
		return result
	}

	random := rand.New(rand.NewSource(r.options.Seed))
	result.RandomCases = r.options.CasesMin + random.Intn(r.options.CasesMax-r.options.CasesMin+1)
	var restart func(context.Context) error
	if r.options.Lifecycle != nil {
		restart = func(restartContext context.Context) error {
			if err := r.options.Lifecycle.Stop(restartContext, true); err != nil {
				return fmt.Errorf("crash candidate: %w", err)
			}
			if err := r.waitStopped(restartContext, runtime, probes); err != nil {
				return err
			}
			if err := r.options.Lifecycle.Start(restartContext); err != nil {
				return fmt.Errorf("restart candidate: %w", err)
			}
			return r.waitReady(restartContext, runtime, probes)
		}
	}
	err = application.Check(ctx, runtime, api.AccuracyContext{
		Seed:    r.options.Seed,
		Cases:   result.RandomCases,
		Restart: restart,
	}, recorder)
	result.Checks, result.Properties = recorder.snapshot()
	if err == nil {
		err = recorder.validateRequired()
	}
	if err == nil && result.Checks == 0 {
		err = fmt.Errorf("accuracy application completed without recording checks")
	}
	if err != nil {
		result.Error = err.Error()
		return result
	}
	result.Valid = true
	return result
}

func (r *Runner) waitReady(
	ctx context.Context,
	runtime api.Runtime,
	probes []api.ReadinessProbe,
) error {
	deadline := time.Now().Add(r.options.StartupTimeout)
	last := "not attempted"
	for {
		allReady := true
		for _, probe := range probes {
			probeContext, cancel := context.WithTimeout(ctx, r.options.ProbeTimeout)
			result := runtime.Invoke(probeContext, probe.Invocation)
			cancel()
			if err := probe.Validate(result); err != nil {
				allReady = false
				last = fmt.Sprintf("%s: %v", probe.Name, err)
				break
			}
		}
		if allReady {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("candidate did not become ready: %s", last)
		}
		if err := sleepContext(ctx, r.options.ProbeInterval); err != nil {
			return err
		}
	}
}

func (r *Runner) waitStopped(
	ctx context.Context,
	runtime api.Runtime,
	probes []api.ReadinessProbe,
) error {
	deadline := time.Now().Add(r.options.StartupTimeout)
	for {
		serving := make([]string, 0)
		for _, probe := range probes {
			probeContext, cancel := context.WithTimeout(ctx, r.options.ProbeTimeout)
			result := runtime.Invoke(probeContext, probe.Invocation)
			cancel()
			if result.TransportSuccess {
				serving = append(serving, probe.Name)
			}
		}
		if len(serving) == 0 {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("candidate endpoints remained reachable after stop: %v", serving)
		}
		if err := sleepContext(ctx, r.options.ProbeInterval); err != nil {
			return err
		}
	}
}

func validateProbes(probes []api.ReadinessProbe) error {
	if len(probes) == 0 {
		return fmt.Errorf("accuracy application declares no readiness probes")
	}
	seen := make(map[string]struct{}, len(probes))
	for index, probe := range probes {
		if probe.Name == "" {
			return fmt.Errorf("readiness probe %d has an empty name", index)
		}
		if probe.Validate == nil {
			return fmt.Errorf("readiness probe %q has no validator", probe.Name)
		}
		if _, duplicate := seen[probe.Name]; duplicate {
			return fmt.Errorf("readiness probe %q is duplicated", probe.Name)
		}
		seen[probe.Name] = struct{}{}
	}
	return nil
}

func seedHash(seed int64) string {
	digest := sha256.Sum256([]byte(strconv.FormatInt(seed, 10)))
	return fmt.Sprintf("%x", digest[:])
}

func sleepContext(ctx context.Context, duration time.Duration) error {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
