package accuracy

import (
	"context"
	"crypto/sha256"
	"fmt"
	"strconv"
	"time"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/probing"
	"vibesys/microservice-evaluator/registry"
	"vibesys/microservice-evaluator/sampling"
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
	CleanupTimeout time.Duration
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
	if options.CleanupTimeout == 0 {
		options.CleanupTimeout = options.StartupTimeout
	}
	if options.CleanupTimeout < 0 {
		return nil, fmt.Errorf("accuracy cleanup timeout must be positive")
	}
	return &Runner{registry: registry, options: options}, nil
}

func (r *Runner) Run(ctx context.Context, workload api.Workload) (result Result) {
	defer func() {
		if recovered := recover(); recovered != nil {
			result.Valid = false
			result.Error = fmt.Sprintf("accuracy evaluation panicked: %v", recovered)
		}
	}()
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
	preflight := application.PreflightProbes()
	if err := probing.Validate(probes, workload.Targets, true); err != nil {
		result.Error = fmt.Sprintf("invalid readiness probes: %v", err)
		return result
	}
	if err := probing.Validate(preflight, workload.Targets, false); err != nil {
		result.Error = fmt.Sprintf("invalid preflight probes: %v", err)
		return result
	}
	casePolicy := application.CasePolicy()
	if casePolicy.MinimumCases < 1 {
		result.Error = fmt.Sprintf(
			"accuracy application case minimum must be positive, got %d",
			casePolicy.MinimumCases,
		)
		return result
	}
	if casePolicy.RandomExtraCases < 0 {
		result.Error = fmt.Sprintf(
			"accuracy application random extra cases must be non-negative, got %d",
			casePolicy.RandomExtraCases,
		)
		return result
	}
	preflightProperties := application.PreflightProperties()
	if len(preflightProperties) == 0 {
		result.Error = "accuracy application declares no preflight properties"
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
	probeOptions := probing.Options{
		PhaseTimeout: r.options.StartupTimeout,
		ProbeTimeout: r.options.ProbeTimeout,
		Interval:     r.options.ProbeInterval,
	}
	if err := probing.WaitReady(ctx, runtime, probes, probeOptions); err != nil {
		result.Error = err.Error()
		return result
	}
	if err := probing.Run(ctx, runtime, preflight, probeOptions); err != nil {
		result.Error = err.Error()
		return result
	}
	recorder.AddChecks(len(preflight))
	if err := recorder.Pass(preflightProperties...); err != nil {
		result.Error = err.Error()
		return result
	}

	minimumCases := max(r.options.CasesMin, casePolicy.MinimumCases)
	maximumCases := max(r.options.CasesMax, minimumCases+casePolicy.RandomExtraCases)
	result.RandomCases, err = sampling.CaseCount(r.options.Seed, minimumCases, maximumCases)
	if err != nil {
		result.Error = err.Error()
		return result
	}
	var restart func(context.Context) error
	if r.options.Lifecycle != nil {
		restart = func(restartContext context.Context) error {
			if err := r.options.Lifecycle.Stop(restartContext, true); err != nil {
				return fmt.Errorf("crash candidate: %w", err)
			}
			if err := probing.WaitStopped(restartContext, runtime, probes, probeOptions); err != nil {
				return err
			}
			if err := r.options.Lifecycle.Start(restartContext); err != nil {
				return fmt.Errorf("restart candidate: %w", err)
			}
			return probing.WaitReady(restartContext, runtime, probes, probeOptions)
		}
	}
	err = application.Check(ctx, runtime, api.AccuracyContext{
		Seed:           r.options.Seed,
		Cases:          result.RandomCases,
		CleanupTimeout: r.options.CleanupTimeout,
		Restart:        restart,
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

func seedHash(seed int64) string {
	digest := sha256.Sum256([]byte(strconv.FormatInt(seed, 10)))
	return fmt.Sprintf("%x", digest[:])
}
