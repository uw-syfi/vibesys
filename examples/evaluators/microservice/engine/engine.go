package engine

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/registry"
	"vibesys/microservice-evaluator/transport"
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

func (e *Engine) Run(ctx context.Context, workload api.Workload) (RunResult, error) {
	application, err := e.registry.Application(workload)
	if err != nil {
		return RunResult{}, err
	}
	runtime, err := transport.Open(ctx, e.registry, workload.Targets)
	if err != nil {
		return RunResult{}, err
	}
	defer runtime.Close()

	result := RunResult{
		Summary: Summary{
			SchemaVersion: ResultSchemaVersion,
			EngineVersion: e.options.EngineVersion,
			WorkloadName:  workload.Name,
			WorkloadHash:  e.options.WorkloadHash,
			Seed:          strconv.FormatInt(workload.Load.Seed, 10),
			PrimaryMetric: workload.Objective,
			Constraints: ConstraintResult{
				Passed:               true,
				MinSuccessRate:       workload.Constraints.MinSuccessRate,
				MaxErrorRate:         workload.Constraints.MaxErrorRate,
				MinOperationsPerType: workload.Constraints.MinOperationsPerType,
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
		prepared := false
		if !e.options.SkipPrepare {
			dataset, err = application.Prepare(ctx, runtime, trialContext)
			if err != nil {
				return RunResult{}, fmt.Errorf("prepare trial %d: %w", trialIndex, err)
			}
			prepared = true
		}
		cleanup := func(prior error) error {
			if !prepared {
				return prior
			}
			prepared = false
			resetErr := application.Reset(ctx, runtime, trialContext)
			if resetErr == nil {
				return prior
			}
			if prior != nil {
				return fmt.Errorf("%w (fixture cleanup: %v)", prior, resetErr)
			}
			return fmt.Errorf("fixture cleanup: %w", resetErr)
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
				return RunResult{}, cleanup(fmt.Errorf("warmup trial %d: %w", trialIndex, err))
			}
			if !warmupGenerator.Sustained {
				return RunResult{}, cleanup(fmt.Errorf(
					"warmup trial %d did not sustain offered load: %.2f < %.2f operations/s",
					trialIndex,
					warmupGenerator.OfferedRate,
					warmupGenerator.MinOfferedRate,
				))
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
			return RunResult{}, cleanup(fmt.Errorf("measure trial %d: %w", trialIndex, err))
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
		if err := cleanup(nil); err != nil {
			return RunResult{}, fmt.Errorf("reset trial %d after measurement: %w", trialIndex, err)
		}
	}

	result.Summary.Aggregate = aggregate(primaryValues)
	result.Summary.Valid = allValid && len(primaryValues) == workload.Load.Repetitions
	if result.Summary.Valid {
		result.Summary.PrimaryValue = result.Summary.Aggregate.Median
	}
	return result, nil
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
	runtime *transport.Runtime,
	dataset any,
	durationSeconds float64,
	seed int64,
) ([]api.Observation, GeneratorReport, error) {
	if workload.Load.Model == "closed_loop" {
		return e.runClosedLoopPhase(ctx, phase, trial, workload, application, runtime, dataset, durationSeconds, seed)
	}
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
		TargetRate:          workload.Load.Rate,
		OfferedRate:         offeredRate,
		MinOfferedRate:      minimum,
		SubmittedOperations: requestCount,
		MaxQueueDepth:       maxQueueDepth,
		SchedulerLagMS:      distribution(schedulerLags),
		Sustained:           offeredRate >= minimum,
	}
	return collected, report, nil
}

func (e *Engine) runClosedLoopPhase(
	ctx context.Context,
	phase api.Phase,
	trial int,
	workload api.Workload,
	application api.Application,
	runtime *transport.Runtime,
	dataset any,
	durationSeconds float64,
	seed int64,
) ([]api.Observation, GeneratorReport, error) {
	duration := time.Duration(durationSeconds * float64(time.Second))
	ready := sync.WaitGroup{}
	workers := sync.WaitGroup{}
	var counter atomic.Int64
	collectedByWorker := make([][]api.Observation, workload.Load.Concurrency)
	start := time.Now().Add(10 * time.Millisecond)
	stop := start.Add(duration)
	for worker := 0; worker < workload.Load.Concurrency; worker++ {
		ready.Add(1)
		workers.Add(1)
		go func(workerIndex int) {
			defer workers.Done()
			rng := rand.New(rand.NewSource(seed ^ int64(workerIndex+1)*0x5deece66d))
			selector := newOperationSelector(workload.Operations)
			ready.Done()
			if err := sleepUntil(ctx, start); err != nil {
				return
			}
			local := make([]api.Observation, 0, 1024)
			for time.Now().Before(stop) {
				sequence := counter.Add(1) - 1
				scheduled := time.Now()
				local = append(local, executeRequest(
					ctx,
					phase,
					trial,
					workload.Load,
					scheduledSample{
						operation: selector.choose(rng.Intn(selector.total)),
						sample:    api.Sample{Counter: sequence, Random: rng.Uint64()},
						scheduled: scheduled,
					},
					application,
					runtime,
					dataset,
				))
			}
			collectedByWorker[workerIndex] = local
		}(worker)
	}
	ready.Wait()
	workers.Wait()
	if err := ctx.Err(); err != nil {
		return nil, GeneratorReport{}, err
	}
	count := int(counter.Load())
	collected := make([]api.Observation, 0, count)
	for _, observations := range collectedByWorker {
		collected = append(collected, observations...)
	}
	elapsed := time.Since(start).Seconds()
	offeredRate := 0.0
	if elapsed > 0 {
		offeredRate = float64(count) / elapsed
	}
	return collected, GeneratorReport{
		OfferedRate:         offeredRate,
		SubmittedOperations: count,
		SchedulerLagMS:      distribution(nil),
		Sustained:           true,
	}, nil
}

func executeRequest(
	parent context.Context,
	phase api.Phase,
	trial int,
	load api.Load,
	item scheduledSample,
	application api.Application,
	runtime *transport.Runtime,
	dataset any,
) api.Observation {
	dispatched := time.Now()
	plan, buildErr := application.BuildOperation(item.operation, item.sample, dataset)
	if buildErr == nil {
		defer application.FinishOperation(plan)
		if len(plan.Invocations) == 0 {
			buildErr = fmt.Errorf("operation %q produced no invocations", item.operation.Name)
		}
	}
	sent := time.Now()
	results := make([]api.ProtocolResult, 0, len(plan.Invocations))
	if buildErr == nil {
		timeout := time.Duration(load.TimeoutSeconds * float64(time.Second))
		for _, invocation := range plan.Invocations {
			ctx, cancel := context.WithTimeout(parent, timeout)
			results = append(results, runtime.Invoke(ctx, invocation))
			cancel()
		}
	}
	completed := time.Now()
	validation := api.ValidationResult{}
	if buildErr == nil {
		validation = application.ValidateOperation(item.operation, plan, results)
	}
	validated := time.Now()
	transportSuccess := buildErr == nil
	var requestBytes, responseBytes int64
	nativeStatuses := make([]string, 0, len(results))
	errorCategory := ""
	errorMessage := ""
	for _, result := range results {
		requestBytes += result.RequestBytes
		responseBytes += result.ResponseBytes
		if result.NativeStatus != "" {
			nativeStatuses = append(nativeStatuses, result.NativeStatus)
		}
		if !result.TransportSuccess {
			transportSuccess = false
			if errorCategory == "" {
				errorCategory = result.ErrorCategory
				errorMessage = result.ErrorMessage
			}
		}
	}
	if buildErr != nil {
		errorCategory = "request_build"
		errorMessage = buildErr.Error()
	}
	success := buildErr == nil && transportSuccess && validation.Success
	if buildErr == nil && transportSuccess && !validation.Success {
		errorCategory = validation.ErrorCategory
		errorMessage = validation.ErrorMessage
	}
	nativeStatus := ""
	if len(nativeStatuses) > 0 {
		nativeStatus = strings.Join(nativeStatuses, ",")
	}
	observation := api.Observation{
		Trial:              trial,
		Phase:              phase,
		Operation:          item.operation.Name,
		Target:             item.operation.Target,
		Protocol:           protocolFor(runtime, item.operation.Target),
		Tags:               append([]string(nil), item.operation.Tags...),
		ScheduledAt:        item.scheduled,
		DispatchedAt:       dispatched,
		SentAt:             sent,
		CompletedAt:        completed,
		ValidatedAt:        validated,
		TransportSuccess:   transportSuccess,
		ApplicationSuccess: success,
		NativeStatus:       nativeStatus,
		NativeStatuses:     nativeStatuses,
		ErrorCategory:      errorCategory,
		ErrorMessage:       errorMessage,
		InvocationCount:    len(results),
		RequestBytes:       requestBytes,
		ResponseBytes:      responseBytes,
		CustomTimings:      validation.CustomTimings,
	}
	observation.PopulateDurations()
	return observation
}

func protocolFor(runtime *transport.Runtime, target string) string {
	protocol, _ := runtime.Protocol(target)
	return protocol
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
