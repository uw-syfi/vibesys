package flow

import (
	"fmt"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// Plan is everything a check or a measure job needs to run. The daemon makes it when the job
// starts and the supervisor only follows it: nothing in a plan is decided by the process that
// carries it out.
type Plan struct {
	Kind core.JobKind
	// Task, TaskVersion, Key and Environment are what the saved result records. Key and
	// Environment are the job's own.
	Task        string
	TaskVersion string
	Key         core.JobKey
	Environment core.Fingerprint

	// Build runs first when the task has one. Check runs in every check job and in a measure job
	// that has no pass to link. Benchmark and Result are for a measure job.
	Build     *task.Command
	Check     task.Command
	Benchmark task.Command
	Result    task.Result
	Metric    core.Metric

	// Samples is how many benchmark runs this job takes now: what the cache lacks, not the count
	// the submission asked for. It is zero for a check job.
	Samples uint32
	// CheckPass is the saved check pass a measure job links instead of running the check. Empty
	// means the job runs the check itself.
	CheckPass core.JobID
}

// RunsCheck reports whether the job runs the check.
func (p Plan) RunsCheck() bool { return p.Kind == core.JobCheck || p.CheckPass == "" }

// Validate reports why a plan cannot be followed, naming the field. A plan that passes can be
// stepped; a plan that does not never starts.
func (p Plan) Validate() error {
	bad := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: "plan: " + fmt.Sprintf(format, args...)}
	}
	switch p.Kind {
	case core.JobCheck:
		if p.Samples != 0 || p.CheckPass != "" {
			return bad("a check job has no samples and links no pass")
		}
	case core.JobMeasure:
		if p.Samples == 0 {
			return bad("a measure job takes at least one sample")
		}
		if p.Benchmark.Argv == nil {
			return bad("a measure job has a benchmark command")
		}
		if p.Result.OutputFlag() == "" || (!p.Result.Protocol2 && p.Result.Field == "") {
			return bad("a measure job says how its benchmark reports a result")
		}
		if p.Metric.Name == "" || (p.Metric.Direction != core.Maximize && p.Metric.Direction != core.Minimize) {
			return bad("a measure job has a metric with a direction")
		}
	default:
		return bad("kind %q is neither check nor measure", p.Kind)
	}
	if p.Task == "" || p.TaskVersion == "" || p.Key == "" {
		return bad("the task, its version and the job key are all required")
	}
	if p.RunsCheck() {
		if err := validCommand("check", p.Check); err != nil {
			return bad("%v", err)
		}
	}
	if p.Kind == core.JobMeasure {
		if err := validCommand("benchmark", p.Benchmark); err != nil {
			return bad("%v", err)
		}
	}
	if p.Build != nil {
		if err := validCommand("build", *p.Build); err != nil {
			return bad("%v", err)
		}
	}
	return nil
}

func validCommand(name string, c task.Command) error {
	if len(c.Argv) == 0 || c.Argv[0] == "" {
		return fmt.Errorf("the %s command has no program", name)
	}
	if c.Timeout <= 0 {
		return fmt.Errorf("the %s command has no positive time limit (%v)", name, c.Timeout)
	}
	return nil
}

// Stage is the step of the flow a request or an ending belongs to.
type Stage string

// The stages, in the order a job takes them.
const (
	StageCheckout  Stage = "checkout"
	StageBuild     Stage = "build"
	StageCheck     Stage = "check"
	StageBenchmark Stage = "benchmark"
	StageObserve   Stage = "observe"
)

// Findings is what a job that passed found out.
type Findings struct {
	// CheckTook is how long the check ran, for a check job.
	CheckTook time.Duration
	// Samples are the scored values, one per benchmark run, in order.
	Samples []float64
	// Observations are the other metrics the benchmark reported, by name, one value per run
	// that reported it.
	Observations []core.Observation
	Machine      core.MachineFacts
	Load         core.MachineLoad
}

// Record is the result to save for a job that passed. A measure job names itself as its check pass
// when it ran the check and the saved pass it linked otherwise.
func (p Plan) Record(job core.JobID, commit core.Commit, f Findings, at time.Time) core.Result {
	r := core.Result{
		Job: job, Key: p.Key, Task: p.Task, TaskVersion: p.TaskVersion, Commit: commit,
		SchemaVersion: core.SchemaVersion, Environment: p.Environment,
		Machine: f.Machine, Load: f.Load, RecordedAt: at,
	}
	if p.Kind == core.JobCheck {
		r.Outcome = core.CheckOutcome{Duration: f.CheckTook}
		return r
	}
	checkJob := p.CheckPass
	if p.RunsCheck() {
		checkJob = job
	}
	r.Outcome = core.MeasureOutcome{Metric: p.Metric, Samples: f.Samples, Observations: f.Observations, CheckJob: checkJob}
	return r
}
