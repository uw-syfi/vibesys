package evaluator

import (
	"context"
	"errors"
	"fmt"
	"os"
	"runtime/debug"
	"slices"
	"strings"
	"syscall"
)

// Mode is what one invocation of the task script does.
type Mode string

// The three modes of a sample (docs/spec.md, "The sample"):
//
//   - Benchmark: a hello record first, then exactly one outcome on --vs-output: the result the
//     task reports, or an error record naming what failed.
//   - Check: no records; the exit status is the verdict (0 passes).
//   - Profile: the standard load for a profiler, never scored; its files go to the artifacts
//     directory, which vibesys-cli copies back.
const (
	Check     Mode = "check"
	Benchmark Mode = "benchmark"
	Profile   Mode = "profile"
)

// Log writes one progress line on standard error.
func Log(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "vibesys-evaluator: %s\n", fmt.Sprintf(format, args...))
}

// SampleError is a sample that is not a measurement: the message is what the error record says.
// Task code returns it (or wraps it) for the failures it expects, so the record names what failed
// without a stack trace; Partial is what was measured before the failure, if anything.
type SampleError struct {
	Msg     string
	Partial *Partial
}

func (e *SampleError) Error() string { return e.Msg }

// Run is one sample, as the task's functions see it: its seed, its phases and its report.
type Run struct {
	mode      Mode
	seed      uint64
	artifacts string
	records   *Stream
	partial   *Partial
}

// Mode is what this invocation does.
func (r *Run) Mode() Mode { return r.mode }

// Seed is the sample seed (seeds.json in the artifacts records it).
func (r *Run) Seed() uint64 { return r.seed }

// Artifacts is the directory kept with the job.
func (r *Run) Artifacts() string { return r.artifacts }

// SetPartial records what was last measured, for the error record of a sample that fails.
func (r *Run) SetPartial(p *Partial) { r.partial = p }

// Phase starts the phase name: a new, empty directory for its files, kept with the job. Names are
// unique in the sample; 1 to 63 of a-z, 0-9, '.', '_' and '-'.
func (r *Run) Phase(name string) (string, error) { return PhaseDir(r.artifacts, name) }

// Report reports the sample's metrics (benchmark mode, once): every declared metric, finite.
func (r *Run) Report(values map[string]float64) error {
	if r.records == nil {
		return fmt.Errorf("run.report: the %s mode is never scored; report only in benchmark", r.mode)
	}
	if err := r.records.Result(values); err != nil {
		var re *RecordError
		if errors.As(err, &re) {
			return fmt.Errorf("run.report: %w", err)
		}
		return err
	}
	return nil
}

// Task is everything a task script passes to Main. Benchmark is required; the others are optional
// and a mode without a function exits 2.
type Task struct {
	// Metrics the benchmark reports, by name.
	Metrics map[string]Metric
	// Benchmark is the scoring definition: it runs phases and calls run.Report. It ends the
	// sample with an error record when it returns an error.
	Benchmark func(ctx context.Context, run *Run) error
	// Check is the correctness check: nil when the system is correct, else what is wrong.
	Check func(ctx context.Context, run *Run) error
	// Profile runs the standard load a profiler records; it is never scored.
	Profile func(ctx context.Context, run *Run) error
}

func (t Task) orderedMetrics() []NamedMetric {
	out := make([]NamedMetric, 0, len(t.Metrics))
	for name, m := range t.Metrics {
		out = append(out, NamedMetric{Name: name, Metric: m})
	}
	slices.SortFunc(out, func(a, b NamedMetric) int { return strings.Compare(a.Name, b.Name) })
	return out
}

func newRun(mode Mode, seedFlag string, hasFlag bool, env func(string) string, scratch string) (*Run, error) {
	seed, source, err := SampleSeed(seedFlag, hasFlag, env(SampleSeedEnv))
	if err != nil {
		return nil, err
	}
	artifacts, err := ArtifactsDir(env(ArtifactsEnv), scratch)
	if err != nil {
		return nil, err
	}
	if err := WriteSeeds(artifacts, seed, source); err != nil {
		return nil, err
	}
	Log("%s sample, seed %d (%s)", mode, seed, source)
	return &Run{mode: mode, seed: seed, artifacts: artifacts}, nil
}

// call runs a task function, turning a panic into an error (with its stack on stderr).
func call(ctx context.Context, fn func(context.Context, *Run) error, run *Run) (err error) {
	defer func() {
		if p := recover(); p != nil {
			fmt.Fprintf(os.Stderr, "panic: %v\n%s", p, debug.Stack())
			err = fmt.Errorf("panic: %v", p)
		}
	}()
	return fn(ctx, run)
}

// stopper turns the first stop signal into a cancelled context and remembers which it was.
type stopper struct {
	ctx    context.Context
	signal func() syscall.Signal
}

func signalName(s syscall.Signal) string {
	switch s {
	case syscall.SIGTERM:
		return "SIGTERM"
	case syscall.SIGINT:
		return "SIGINT"
	case syscall.SIGHUP:
		return "SIGHUP"
	}
	return s.String()
}

// benchmarkSample measures one sample and writes its records to output. It returns the exit
// status: 0 when the stream ends with a result.
func benchmarkSample(st *stopper, task Task, output, seedFlag string, hasFlag bool, env func(string) string) int {
	records := NewStream(output)
	defer records.Close()
	if err := records.Hello(task.orderedMetrics()); err != nil {
		_ = records.Error("main(metrics=...): "+err.Error(), nil)
		return 1
	}
	scratch, err := os.MkdirTemp("", "vibesys-evaluator-")
	if err != nil {
		_ = records.Error(err.Error(), nil)
		return 1
	}
	defer os.RemoveAll(scratch)
	var run *Run
	partial := func() *Partial {
		if run != nil {
			return run.partial
		}
		return nil
	}
	fail := func(message string, p *Partial) {
		if p == nil {
			p = partial()
		}
		_ = records.Error(message, p)
	}
	run, err = newRun(Benchmark, seedFlag, hasFlag, env, scratch)
	if err == nil {
		run.records = records
		err = call(st.ctx, task.Benchmark, run)
	}
	switch sig := st.signal(); {
	case sig != 0:
		Log("stopped by signal %d", int(sig))
		fail(fmt.Sprintf("stopped by signal %s before the sample finished", signalName(sig)), nil)
	case err != nil:
		var se *SampleError
		if errors.As(err, &se) {
			Log("failed: %s", se.Msg)
			fail(err.Error(), se.Partial)
		} else {
			Log("failed: %v", err)
			fail(err.Error(), nil)
		}
	case records.Outcome() == NoOutcome:
		fail("benchmark(run) returned without calling run.report(...)", nil)
	}
	if records.Outcome() == OutcomeResult {
		return 0
	}
	return 1
}

// unscoredSample runs a check or profile sample: exit 0 when body returns nil, else 1 with its
// reason; a stop signal exits 128 plus its number.
func unscoredSample(st *stopper, mode Mode, seedFlag string, hasFlag bool, env func(string) string, body func(context.Context, *Run) error) int {
	scratch, err := os.MkdirTemp("", "vibesys-evaluator-")
	if err != nil {
		Log("%s failed: %v", mode, err)
		return 1
	}
	defer os.RemoveAll(scratch)
	run, err := newRun(mode, seedFlag, hasFlag, env, scratch)
	if err == nil {
		err = call(st.ctx, body, run)
	}
	if sig := st.signal(); sig != 0 {
		return 128 + int(sig)
	}
	if err != nil {
		Log("%s failed: %v", mode, err)
		return 1
	}
	Log("%s: ok", mode)
	return 0
}
