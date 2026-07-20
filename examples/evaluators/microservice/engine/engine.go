package engine

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"sync"
	"time"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/registry"
)

type Options struct {
	EngineVersion string
	WorkloadHash  string
	SkipPrepare   bool
}

type Engine struct {
	registry *registry.Registry
	options  Options
}

func New(registry *registry.Registry, options Options) *Engine {
	if options.EngineVersion == "" {
		options.EngineVersion = "dev"
	}
	return &Engine{registry: registry, options: options}
}

type runtime struct {
	clients   map[string]api.Client
	protocols map[string]string
}

func (r *runtime) Invoke(ctx context.Context, invocation api.Invocation) api.ProtocolResult {
	client, ok := r.clients[invocation.Target]
	if !ok {
		return api.ProtocolResult{
			ErrorCategory: "unknown_target",
			ErrorMessage:  fmt.Sprintf("invocation references unknown target %q", invocation.Target),
		}
	}
	return client.Invoke(ctx, invocation)
}

func (r *runtime) close() error {
	var first error
	for target, client := range r.clients {
		if err := client.Close(); err != nil && first == nil {
			first = fmt.Errorf("close target %q: %w", target, err)
		}
	}
	return first
}

func (e *Engine) Run(ctx context.Context, workload api.Workload) (RunResult, error) {
	application, err := e.registry.Application(workload)
	if err != nil {
		return RunResult{}, err
	}
	runtime, err := e.openTargets(ctx, workload.Targets)
	if err != nil {
		return RunResult{}, err
	}
	defer runtime.close()

	result := RunResult{
		Summary: Summary{
			SchemaVersion: ResultSchemaVersion,
			EngineVersion: e.options.EngineVersion,
			WorkloadName:  workload.Name,
			WorkloadHash:  e.options.WorkloadHash,
			PrimaryMetric: workload.Objective,
			Constraints: ConstraintResult{
				Passed:         true,
				MinSuccessRate: workload.Constraints.MinSuccessRate,
				MaxErrorRate:   workload.Constraints.MaxErrorRate,
			},
		},
	}
	primaryValues := make([]float64, 0, workload.Load.Repetitions)
	allValid := true

	for trialIndex := 0; trialIndex < workload.Load.Repetitions; trialIndex++ {
		trialContext := api.TrialContext{Index: trialIndex, Seed: workload.Load.Seed + int64(trialIndex)}
		if err := application.Reset(ctx, runtime, trialContext); err != nil {
			return RunResult{}, fmt.Errorf("reset trial %d: %w", trialIndex, err)
		}
		var dataset any
		if !e.options.SkipPrepare {
			dataset, err = application.Prepare(ctx, runtime, trialContext)
			if err != nil {
				return RunResult{}, fmt.Errorf("prepare trial %d: %w", trialIndex, err)
			}
		}

		if workload.Load.WarmupSeconds > 0 {
			_, warmupGenerator, err := e.runPhase(
				ctx,
				api.PhaseWarmup,
				trialIndex,
				workload,
				application,
				runtime,
				dataset,
				workload.Load.WarmupSeconds,
				workload.Load.Seed+int64(trialIndex*2),
			)
			if err != nil {
				return RunResult{}, fmt.Errorf("warmup trial %d: %w", trialIndex, err)
			}
			if !warmupGenerator.Sustained {
				return RunResult{}, fmt.Errorf(
					"warmup trial %d did not sustain offered load: %.2f < %.2f requests/s",
					trialIndex,
					warmupGenerator.OfferedRate,
					warmupGenerator.MinOfferedRate,
				)
			}
		}

		observations, generator, err := e.runPhase(
			ctx,
			api.PhaseMeasurement,
			trialIndex,
			workload,
			application,
			runtime,
			dataset,
			workload.Load.DurationSeconds,
			workload.Load.Seed+int64(trialIndex*2+1),
		)
		if err != nil {
			return RunResult{}, fmt.Errorf("measure trial %d: %w", trialIndex, err)
		}
		result.Observations = append(result.Observations, observations...)
		trial := summarizeTrial(trialIndex, observations, generator, workload)
		result.Summary.Trials = append(result.Summary.Trials, trial)
		if trial.PrimaryValue != nil {
			primaryValues = append(primaryValues, *trial.PrimaryValue)
		}
		if !trial.Valid {
			allValid = false
			result.Summary.Constraints.Passed = false
			for _, reason := range trial.InvalidReasons {
				result.Summary.Constraints.Reasons = append(
					result.Summary.Constraints.Reasons,
					fmt.Sprintf("trial %d: %s", trialIndex, reason),
				)
			}
		}
	}

	result.Summary.Aggregate = aggregate(primaryValues)
	result.Summary.Valid = allValid && len(primaryValues) == workload.Load.Repetitions
	if result.Summary.Valid {
		result.Summary.PrimaryValue = result.Summary.Aggregate.Median
	}
	return result, nil
}

func (e *Engine) openTargets(ctx context.Context, targets []api.Target) (*runtime, error) {
	runtime := &runtime{
		clients:   make(map[string]api.Client, len(targets)),
		protocols: make(map[string]string, len(targets)),
	}
	for _, target := range targets {
		driver, err := e.registry.Driver(target.Protocol)
		if err != nil {
			runtime.close()
			return nil, fmt.Errorf("target %q: %w", target.Name, err)
		}
		client, err := driver.Open(ctx, target)
		if err != nil {
			runtime.close()
			return nil, fmt.Errorf("open target %q: %w", target.Name, err)
		}
		runtime.clients[target.Name] = client
		runtime.protocols[target.Name] = target.Protocol
	}
	return runtime, nil
}

type scheduledSample struct {
	operation api.Operation
	sample    api.Sample
	scheduled time.Time
}

func (e *Engine) runPhase(
	ctx context.Context,
	phase api.Phase,
	trial int,
	workload api.Workload,
	application api.Application,
	runtime *runtime,
	dataset any,
	durationSeconds float64,
	seed int64,
) ([]api.Observation, GeneratorReport, error) {
	requestCount := int(math.Ceil(workload.Load.Rate * durationSeconds))
	if requestCount <= 0 {
		return nil, GeneratorReport{}, fmt.Errorf("phase %s schedules no requests", phase)
	}
	queueCapacity := workload.Load.Concurrency * 4
	work := make(chan scheduledSample, queueCapacity)
	observations := make(chan api.Observation, requestCount)
	ready := sync.WaitGroup{}
	workers := sync.WaitGroup{}
	for index := 0; index < workload.Load.Concurrency; index++ {
		ready.Add(1)
		workers.Add(1)
		go func() {
			defer workers.Done()
			ready.Done()
			for item := range work {
				observations <- executeRequest(
					ctx,
					phase,
					trial,
					workload.Load,
					item,
					application,
					runtime,
					dataset,
				)
			}
		}()
	}
	ready.Wait()

	start := time.Now()
	rng := rand.New(rand.NewSource(seed))
	selector := newOperationSelector(workload.Operations)
	schedulerLags := make([]float64, 0, requestCount)
	maxQueueDepth := 0
	var lastSubmitted time.Time
	for index := 0; index < requestCount; index++ {
		scheduled := start.Add(time.Duration(float64(index) / workload.Load.Rate * float64(time.Second)))
		if err := sleepUntil(ctx, scheduled); err != nil {
			close(work)
			workers.Wait()
			close(observations)
			return nil, GeneratorReport{}, err
		}
		now := time.Now()
		lag := now.Sub(scheduled)
		if lag < 0 {
			lag = 0
		}
		schedulerLags = append(schedulerLags, float64(lag)/float64(time.Millisecond))
		item := scheduledSample{
			operation: selector.choose(rng.Intn(selector.total)),
			sample: api.Sample{
				Counter: int64(index),
				Random:  rng.Uint64(),
			},
			scheduled: scheduled,
		}
		select {
		case <-ctx.Done():
			close(work)
			workers.Wait()
			close(observations)
			return nil, GeneratorReport{}, ctx.Err()
		case work <- item:
			lastSubmitted = time.Now()
			if depth := len(work); depth > maxQueueDepth {
				maxQueueDepth = depth
			}
		}
	}
	close(work)
	workers.Wait()
	close(observations)

	collected := make([]api.Observation, 0, requestCount)
	for observation := range observations {
		collected = append(collected, observation)
	}
	offeredRate := workload.Load.Rate
	if requestCount > 1 {
		span := lastSubmitted.Sub(start).Seconds()
		if span > 0 {
			offeredRate = float64(requestCount-1) / span
		}
	}
	minimum := workload.Load.Rate * workload.Load.MinOfferedRateRatio
	report := GeneratorReport{
		TargetRate:        workload.Load.Rate,
		OfferedRate:       offeredRate,
		MinOfferedRate:    minimum,
		SubmittedRequests: requestCount,
		MaxQueueDepth:     maxQueueDepth,
		SchedulerLagMS:    distribution(schedulerLags),
		Sustained:         offeredRate >= minimum,
	}
	return collected, report, nil
}

func executeRequest(
	parent context.Context,
	phase api.Phase,
	trial int,
	load api.Load,
	item scheduledSample,
	application api.Application,
	runtime *runtime,
	dataset any,
) api.Observation {
	dispatched := time.Now()
	invocation, buildErr := application.BuildInvocation(item.operation, item.sample, dataset)
	sent := time.Now()
	result := api.ProtocolResult{}
	if buildErr != nil {
		result.ErrorCategory = "request_build"
		result.ErrorMessage = buildErr.Error()
	} else {
		timeout := time.Duration(load.TimeoutSeconds * float64(time.Second))
		ctx, cancel := context.WithTimeout(parent, timeout)
		result = runtime.Invoke(ctx, invocation)
		cancel()
	}
	completed := time.Now()
	validation := api.ValidationResult{}
	if buildErr == nil {
		validation = application.Validate(item.operation, result)
	}
	success := buildErr == nil && result.TransportSuccess && validation.Success
	errorCategory := result.ErrorCategory
	errorMessage := result.ErrorMessage
	if result.TransportSuccess && !validation.Success {
		errorCategory = validation.ErrorCategory
		errorMessage = validation.ErrorMessage
	}
	observation := api.Observation{
		Trial:              trial,
		Phase:              phase,
		Operation:          item.operation.Name,
		Target:             item.operation.Target,
		Protocol:           runtime.protocols[item.operation.Target],
		Tags:               append([]string(nil), item.operation.Tags...),
		ScheduledAt:        item.scheduled,
		DispatchedAt:       dispatched,
		SentAt:             sent,
		CompletedAt:        completed,
		TransportSuccess:   result.TransportSuccess,
		ApplicationSuccess: success,
		NativeStatus:       result.NativeStatus,
		ErrorCategory:      errorCategory,
		ErrorMessage:       errorMessage,
		RequestBytes:       result.RequestBytes,
		ResponseBytes:      result.ResponseBytes,
		CustomTimings:      validation.CustomTimings,
	}
	observation.PopulateDurations()
	return observation
}

func sleepUntil(ctx context.Context, deadline time.Time) error {
	delay := time.Until(deadline)
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

type operationSelector struct {
	operations []api.Operation
	cumulative []int
	total      int
}

func newOperationSelector(operations []api.Operation) operationSelector {
	selector := operationSelector{operations: operations, cumulative: make([]int, len(operations))}
	for index, operation := range operations {
		selector.total += operation.Weight
		selector.cumulative[index] = selector.total
	}
	return selector
}

func (s operationSelector) choose(value int) api.Operation {
	for index, upper := range s.cumulative {
		if value < upper {
			return s.operations[index]
		}
	}
	return s.operations[len(s.operations)-1]
}
