package microservices

import (
	"context"
	"fmt"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
)

// Run is the sample a microservices Benchmark receives: the core's Run (Seed, Phase, Report) and
// the load phases. Each phase runs in a phase pod next to the deployed system and returns what it
// measured, or a *evaluator.SampleError that names the phase and why it is not a measurement
// (return it: it ends the sample with an error record). Every phase builds its requests from a
// model made afresh, and nothing is reset between phases: the application keeps what earlier
// phases wrote.
type Run struct {
	*evaluator.Run
	phases *phases
}

// ClosedLoop runs a closed-loop phase: capacity, Clients clients each sending its next operation
// when the last completed. The result's latency is end minus start.
func (r *Run) ClosedLoop(ctx context.Context, load Closed) (PhaseResult, error) {
	return r.phase(ctx, load)
}

// OpenLoop runs an open-loop phase: latency at a fixed offered rate. The result's latency is end
// minus the scheduled arrival.
func (r *Run) OpenLoop(ctx context.Context, load Open) (PhaseResult, error) {
	return r.phase(ctx, load)
}

func (r *Run) phase(ctx context.Context, load Load) (PhaseResult, error) {
	result, err := r.phases.run(ctx, load)
	if err == nil {
		r.SetPartial(&evaluator.Partial{Name: "last_phase_ops_per_sec", Value: result.OpsPerSec, Direction: evaluator.Max, Unit: "ops/s"})
	}
	return result, err
}

// Fault runs action between two phases, after at least one, and records it as a fault of kind
// (^[a-z][a-z0-9_]{0,62}$, a name the task's oracle gives meaning: "restart", "flush") on target
// (for messages): no phase runs while it does, and its start and end are taken on the clock loadgen
// stamps its results with. Under capture (Task.Capture) the faults go into the accuracy history
// (history.binpb in the artifacts). An error of action is returned, wrapped, after it is recorded.
func (r *Run) Fault(ctx context.Context, kind, target string, action func(context.Context) error) error {
	return r.phases.fault(ctx, kind, target, action)
}

// phases runs a sample's load phases in order on a deployment and judges each: the Go phase
// runner (phase.Runner) as a benchmark sees it, where a phase that is not a measurement ends the
// sample.
type phases struct {
	runner *phase.Runner
	last   *PhaseResult
}

func (p *phases) partial() *evaluator.Partial {
	if p.last == nil {
		return nil
	}
	return &evaluator.Partial{Name: "last_phase_ops_per_sec", Value: p.last.OpsPerSec, Direction: evaluator.Max, Unit: "ops/s"}
}

func (p *phases) fail(format string, args ...any) error {
	return &evaluator.SampleError{Msg: fmt.Sprintf(format, args...), Partial: p.partial()}
}

func (p *phases) run(ctx context.Context, load Load) (PhaseResult, error) {
	asked, err := load.input()
	if err != nil {
		return PhaseResult{}, err
	}
	ran, err := p.runner.Run(ctx, asked)
	if err != nil {
		return PhaseResult{}, p.fail("%v", err)
	}
	if ran.Verdict.Failure != "" {
		return PhaseResult{}, p.fail("%s", phase.Failure(ran.Input, ran.Verdict.Failure))
	}
	m := ran.Verdict.Measurement
	result := PhaseResult{OpsPerSec: m.OpsPerSec, P50Ms: m.P50Ms, P99Ms: m.P99Ms}
	p.last = &result
	return result, nil
}

func (p *phases) fault(ctx context.Context, kind, target string, action func(context.Context) error) error {
	return p.runner.Fault(ctx, kind, target, action)
}
