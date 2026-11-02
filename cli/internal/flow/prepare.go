package flow

import (
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// Starting is what a plan is made from when a queued job is about to start.
type Starting struct {
	// Job is the queued job, with the key it was submitted under.
	Job core.Job
	// NoCache is the submission's choice to run the whole job with a fresh check.
	NoCache bool
	// Task is the task as the job's commit has it.
	Task task.Task
	// Environment is what the job's environment fingerprints as now, which the job will pin.
	Environment core.Fingerprint
	// Saved holds the saved results of the job's commit, whatever their key.
	Saved []core.Result
}

// Prepare makes the plan of a job that is about to start. It is the rule for what the cache lets
// a job skip, applied at the moment the job runs and not at the moment it was queued, so a
// result saved while the job waited is counted:
//
//   - a check job always runs its check: it has no result of its own to save otherwise;
//   - a measure job takes the samples the cache lacks (core.PlanCache), and at least the count
//     asked for when saved samples already cover it, since a job that succeeds saves a result of
//     its own;
//   - a measure job links the saved check pass of its own key's check, from the same commit, task
//     version and environment, unless the job is no-cache or rechecks, and runs the check itself
//     when there is none.
//
// It fails with FailureEnvironmentMismatch when the key the job was queued under is not the key
// of the commit, task version and environment it would run in now: the task or the environment
// changed while the job waited, and a result saved under the old key would be a lie.
func Prepare(in Starting) (Plan, error) {
	job := in.Job
	if job.Spec.Settings == nil {
		return Plan{}, &core.Failure{Kind: core.FailureInvalidInput, Message: "job: no settings"}
	}
	kind := job.Spec.Settings.Kind()
	if kind != core.JobCheck && kind != core.JobMeasure {
		return Plan{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("job: a %s job has no plan", kind)}
	}
	keyOf := func(kind core.JobKind, settings core.Settings) (core.JobKey, error) {
		return core.NewJobKey(core.KeyInputs{
			Kind: kind, Commit: job.Spec.Commit, TaskVersion: in.Task.Version,
			Environment: in.Environment, Settings: settings, SchemaVersion: core.SchemaVersion,
		})
	}
	key, err := keyOf(kind, job.Spec.Settings)
	if err != nil {
		return Plan{}, err
	}
	if key != job.Key {
		return Plan{}, &core.Failure{Kind: core.FailureEnvironmentMismatch, Message: fmt.Sprintf(
			"the task or the environment changed while job %s waited: it was queued under key %s and would now run under %s", job.ID, job.Key, key)}
	}

	t := in.Task
	p := Plan{
		Kind: kind, Task: t.Name, TaskVersion: t.Version, Key: key, Environment: in.Environment,
		Build: t.Build, Check: t.Check,
	}
	if kind == core.JobCheck {
		return p, nil
	}
	settings := job.Spec.Settings.(core.MeasureSettings)
	p.Benchmark, p.Result, p.Metric = t.Benchmark, t.Result, t.Metric

	cache, err := core.PlanCache(core.CacheQuery{Key: key, Settings: settings, DefaultSamples: t.Samples, NoCache: in.NoCache, Saved: in.Saved})
	if err != nil {
		return Plan{}, err
	}
	p.Samples = cache.Samples
	if cache.Action == core.CacheReuse {
		p.Samples = settings.Samples
		if p.Samples == 0 {
			p.Samples = t.Samples
		}
	}
	checkKey, err := keyOf(core.JobCheck, core.CheckSettings{})
	if err != nil {
		return Plan{}, err
	}
	pass, err := core.PlanCache(core.CacheQuery{Key: checkKey, Settings: core.CheckSettings{}, NoCache: in.NoCache || settings.Recheck, Saved: in.Saved})
	if err != nil {
		return Plan{}, err
	}
	if pass.Action == core.CacheReuse {
		p.CheckPass = pass.From[0]
	}
	return p, nil
}
