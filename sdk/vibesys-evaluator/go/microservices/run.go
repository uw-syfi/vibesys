package microservices

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
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

// The accuracy history's manifest, beside the phase directories and the dataset file in the
// sample's artifacts (docs/spec.md, "Accuracy history").
const historyFile = "history.binpb"

var faultKind = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)

// phases runs a sample's load phases in order on a deployment and judges each: the Go phase
// runner behind Run.
type phases struct {
	pods      pods
	command   func(workDir, target string) []string
	seed      uint64
	artifacts string
	validity  evaluator.Validity
	capture   bool
	log       func(string)
	now       func() time.Time

	next   int
	faults []*pb.FaultEvent
	// windowEnd is the end of the last phase's window on the loadgen clock: a fault that starts
	// before it shows clocks that disagree.
	windowEnd int64
	last      *PhaseResult
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
	index := p.next
	p.next++
	in := phase.Input(p.seed, index, asked, p.capture)
	name := fmt.Sprintf("phase-%d", index)
	dir, err := evaluator.PhaseDir(p.artifacts, name)
	if err != nil {
		return PhaseResult{}, err
	}
	p.log(fmt.Sprintf("phase %d: %s", index, phase.Describe(in)))
	encoded, err := proto.Marshal(in)
	if err != nil {
		return PhaseResult{}, err
	}
	ran, err := p.pods.runPod(ctx, podSpec{
		name: name, command: p.command, outputs: dir, timeout: phase.Timeout(in),
		inputs: map[string][]byte{phase.InputFile: encoded},
	})
	if err != nil {
		_ = os.WriteFile(filepath.Join(dir, phase.LogFile), []byte(err.Error()), 0o644)
		return PhaseResult{}, p.fail("phase %d (%s): %v", index, phase.Describe(in), err)
	}
	_ = os.WriteFile(filepath.Join(dir, phase.LogFile), []byte(ran.log), 0o644)
	if ran.exitCode != 0 {
		return PhaseResult{}, p.fail("%s exited %d after %.0f s; log tail:\n%s", name, ran.exitCode, ran.seconds, tail(ran.log, 1500))
	}
	output, err := phase.ReadOutput(dir)
	if err != nil {
		return PhaseResult{}, p.fail("phase %d (%s) left no usable output: %v", index, phase.Describe(in), err)
	}
	p.windowEnd = output.GetSummary().GetWindowEndUnixNanos()
	if p.capture {
		if err := p.writeHistory(); err != nil {
			return PhaseResult{}, err
		}
	}
	verdict, err := phase.Judge(output, p.validity)
	if err != nil {
		return PhaseResult{}, p.fail("phase %d: %v", index, err)
	}
	for _, line := range phase.Log(in, output, verdict) {
		p.log(line)
	}
	if verdict.Failure != "" {
		return PhaseResult{}, p.fail("%s", phase.Failure(in, verdict.Failure))
	}
	m := verdict.Measurement
	result := PhaseResult{OpsPerSec: m.OpsPerSec, P50Ms: m.P50Ms, P99Ms: m.P99Ms}
	p.last = &result
	return result, nil
}

func (p *phases) fault(ctx context.Context, kind, target string, action func(context.Context) error) error {
	if !faultKind.MatchString(kind) {
		return fmt.Errorf("fault kind %q must match %s", kind, faultKind)
	}
	if p.next == 0 {
		return fmt.Errorf("fault %s: a fault goes between phases; run a phase first", kind)
	}
	after := p.next - 1
	start := p.now()
	if start.UnixNano() < p.windowEnd {
		p.log(fmt.Sprintf("fault %s starts at %s, before phase %d's window ended on loadgen's clock (%s): "+
			"this machine's clock and the phase pod's disagree, and the fault's times are off by as much",
			kind, start.UTC().Format(time.RFC3339Nano), after, time.Unix(0, p.windowEnd).UTC().Format(time.RFC3339Nano)))
	}
	err := action(ctx)
	end := p.now()
	p.faults = append(p.faults, &pb.FaultEvent{
		Kind: kind, Target: target, StartUnixNanos: start.UnixNano(), EndUnixNanos: end.UnixNano(), AfterPhase: uint32(after),
	})
	if p.capture {
		if werr := p.writeHistory(); werr != nil && err == nil {
			err = werr
		}
	}
	if err != nil {
		return fmt.Errorf("fault %s on %s after phase %d: %w", kind, target, after, err)
	}
	p.log(fmt.Sprintf("fault %s on %s after phase %d: %.1f s", kind, target, after, end.Sub(start).Seconds()))
	return nil
}

// writeHistory writes the accuracy history's manifest: the phases run so far, the faults, the
// dataset file.
func (p *phases) writeHistory() error {
	manifest := &pb.HistoryManifest{Phases: uint32(p.next), Faults: p.faults, Dataset: phase.DatasetFile}
	encoded, err := proto.Marshal(manifest)
	if err != nil {
		return err
	}
	path := filepath.Join(p.artifacts, historyFile)
	if err := os.WriteFile(path+".tmp", encoded, 0o644); err != nil {
		return err
	}
	return os.Rename(path+".tmp", path)
}

func tail(text string, n int) string {
	text = strings.TrimSpace(text)
	if len(text) <= n {
		return text
	}
	return text[len(text)-n:]
}
