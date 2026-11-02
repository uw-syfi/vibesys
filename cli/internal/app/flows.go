package app

import (
	"context"
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// keyFlow resolves a check or a measure submission before it is queued: it reads the task as the
// submission's commit has it, decides the environment the job runs in and fingerprints it, and
// puts the sample count a measure asks for into the spec, so the job and its key say what will
// run. The key is the job's cache key. Everything is decided here and not by the store or the
// supervisor, which only keep what they are given.
func (a *App) keyFlow(ctx context.Context, c core.CallContext, spec core.JobSpec) (core.JobSpec, core.JobKey, error) {
	t, err := a.taskAt(ctx, c.Repository, spec.Commit, c.Task)
	if err != nil {
		return spec, "", err
	}
	kind, err := core.ChooseEnvironment(t.Setup(), spec.Environment)
	if err != nil {
		return spec, "", err
	}
	fingerprint, err := a.fingerprintOf(ctx, kind, environment.Target{Repository: c.Repository, Commit: spec.Commit, Task: c.Task})
	if err != nil {
		return spec, "", err
	}
	if m, ok := spec.Settings.(core.MeasureSettings); ok && m.Samples == 0 {
		m.Samples = t.Samples
		spec.Settings = m
	}
	spec.Environment = kind
	key, err := core.NewJobKey(core.KeyInputs{
		Kind: spec.Settings.Kind(), Commit: spec.Commit, TaskVersion: t.Version,
		Environment: fingerprint, Settings: spec.Settings, SchemaVersion: core.SchemaVersion,
	})
	return spec, key, err
}

// planFlow makes the plan of a queued check or measure job that is about to start: the rule for
// what the cache lets it skip, applied now and not when the job was queued (flow.Prepare). It
// fails when the task or the environment is not what the job was keyed under.
func (a *App) planFlow(t *taskEntry, job core.Job, fingerprint core.Fingerprint) (flow.Plan, error) {
	tk, err := a.taskAt(a.ctx, t.scope.Repository, job.Spec.Commit, t.scope.Task)
	if err != nil {
		return flow.Plan{}, err
	}
	saved, err := t.store.Results(a.ctx, core.ResultFilter{Commit: job.Spec.Commit})
	if err != nil {
		return flow.Plan{}, err
	}
	return flow.Prepare(flow.Starting{Job: job, NoCache: job.NoCache, Task: tk, Environment: fingerprint, Saved: saved})
}

// taskAt reads the task as the commit has it. A daemon built without a source of tasks cannot run
// a check or a measure job, and says so instead of guessing at the task.
func (a *App) taskAt(ctx context.Context, repository string, commit core.Commit, name string) (task.Task, error) {
	if a.cfg.Tasks == nil {
		return task.Task{}, &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon has no source of tasks, so it cannot run check or measure jobs"}
	}
	return a.cfg.Tasks.Task(ctx, repository, commit, name)
}

// fingerprintOf fingerprints the environment of a kind for the target the job runs. The kind is
// concrete: a job's environment is decided before it is queued.
func (a *App) fingerprintOf(ctx context.Context, kind core.EnvironmentKind, target environment.Target) (core.Fingerprint, error) {
	if kind == core.EnvironmentAuto {
		return core.Fingerprint{}, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("the environment %q was not decided before the job was keyed", kind)}
	}
	env, err := a.cfg.Environments.Environment(kind, target)
	if err != nil {
		return core.Fingerprint{}, err
	}
	return env.Fingerprint(ctx)
}
