package app

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// keyFlow resolves a check or a measure submission before it is queued: it reads the task as the
// submission's commit has it, decides the environment the job runs in and fingerprints it, and
// puts the sample count a measure asks for into the spec, so the job and its key say what will
// run. It gives the submission the job's cache key, what its task needs of a placement, and the
// task version it is for. Everything is decided here and not by the store or the supervisor, which
// only keep what they are given.
//
// A docker job may run on any placement that gives what its task needs, and where it runs is no
// part of its key, so it is fingerprinted on the first of them by name. The scheduler chooses where
// it runs when it starts, and an engine there that builds the image to another digest gives the job
// the key of that digest then (flow.Prepare). A host job runs on this machine, whatever placement
// it was restricted to, so it is restricted to none.
func (a *App) keyFlow(ctx context.Context, sub core.Submission) (core.Submission, error) {
	c, spec := sub.Context, sub.Spec
	t, err := a.taskAt(ctx, string(c.Scope.Repository()), spec.Commit, string(c.Scope.Task()))
	if err != nil {
		return sub, err
	}
	if err := a.refuseChangedTask(ctx, c.Scope, spec.Commit, t); err != nil {
		return sub, err
	}
	kind, err := core.ChooseEnvironment(t.Setup(), spec.Environment)
	if err != nil {
		return sub, err
	}
	eligible, err := a.eligibleOf(kind, t.Needs, spec.Placement)
	if err != nil {
		return sub, err
	}
	if kind != core.EnvironmentDocker {
		spec.Placement = ""
	}
	fingerprint, err := a.fingerprintOf(ctx, kind, environment.Target{Repository: string(c.Scope.Repository()), Commit: spec.Commit, Task: string(c.Scope.Task()), Placement: eligible[0]})
	if err != nil {
		return sub, err
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
	sub.Spec, sub.Key, sub.Needs, sub.TaskVersion = spec, key, needsOf(kind, t.Needs), t.Version
	return sub, err
}

// placeFlow decides where a command job may run, so that scheduling claims what it runs on. A
// command that leaves its environment to the task runs where the task's check and measure run
// (core.ChooseEnvironment), so on a task with a Dockerfile it is a docker job in the task's image
// and waits behind a measure of its placement like any other docker job there. A docker job may
// run on any placement that gives what its task needs, or on the one it is restricted to, and is
// refused when none does; it records the needs, which the scheduler starts it on a placement by. A
// host job runs on no placement, whatever was asked, as for a check or a measure. The submission
// records the environment, the restriction and the needs. A command job has no key, so nothing is
// fingerprinted, and a host one names no task to read.
func (a *App) placeFlow(ctx context.Context, sub core.Submission) (core.Submission, error) {
	c, spec := sub.Context, sub.Spec
	var needs []core.ResourceKind
	if spec.Environment == core.EnvironmentAuto || spec.Environment == core.EnvironmentDocker && len(a.cfg.Placements) > 0 {
		t, err := a.taskAt(ctx, string(c.Scope.Repository()), spec.Commit, string(c.Scope.Task()))
		if err != nil {
			return sub, err
		}
		if spec.Environment, err = core.ChooseEnvironment(t.Setup(), spec.Environment); err != nil {
			return sub, err
		}
		needs = t.Needs
	}
	if _, err := a.eligibleOf(spec.Environment, needs, spec.Placement); err != nil {
		return sub, err
	}
	if spec.Environment != core.EnvironmentDocker {
		spec.Placement = ""
	}
	sub.Spec, sub.Needs = spec, needsOf(spec.Environment, needs)
	return sub, nil
}

// needsOf is what a job of the kind needs of a placement: what its task needs, for a docker job,
// and nothing for a host job, which runs on no placement.
func needsOf(kind core.EnvironmentKind, needs []core.ResourceKind) []core.ResourceKind {
	if kind != core.EnvironmentDocker {
		return nil
	}
	return needs
}

// maxNamedChanges bounds how many changed paths a refusal names.
const maxNamedChanges = 5

// refuseChangedTask fails a submission whose commit changes the task's files from the base's: the
// commit the result branch is at. A result is only comparable with the base's under one task
// version, so a candidate that edits its checker, its benchmark or anything else under the task's
// directory could never be promoted, and would score under a check it chose for itself. The
// refusal comes here, before any job runs, and names the paths. Generated files such as bytecode
// are not part of the task (task.Generated), so they never cause it.
//
// Without a result branch there is no base to hold the task to, so nothing is refused; the base
// of a run is the commit its result branch is created at, and a task is written before that.
func (a *App) refuseChangedTask(ctx context.Context, scope core.Scope, commit core.Commit, candidate task.Task) error {
	if a.cfg.Branches == nil {
		return nil
	}
	branch, err := core.ResultBranch(string(scope.Task()))
	if err != nil {
		return err
	}
	repository := string(scope.Repository())
	head, err := a.cfg.Branches.Head(ctx, repository, branch)
	var f *core.Failure
	if errors.As(err, &f) && f.Kind == core.FailureNotFound {
		return nil
	}
	if err != nil {
		return err
	}
	if head == commit {
		return nil
	}
	base, err := a.taskAt(ctx, repository, head, string(scope.Task()))
	if errors.As(err, &f) {
		return &core.Failure{Kind: f.Kind, Message: fmt.Sprintf("the result branch %s is at %s, whose task cannot be compared with this commit's: %s", branch, head, f.Message)}
	}
	if err != nil {
		return err
	}
	changes := candidate.ChangesFrom(base)
	if len(changes) == 0 {
		return nil
	}
	named := make([]string, 0, maxNamedChanges)
	for _, c := range changes[:min(len(changes), maxNamedChanges)] {
		named = append(named, c.String())
	}
	more := ""
	if len(changes) > len(named) {
		more = fmt.Sprintf(" and %d more", len(changes)-len(named))
	}
	return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
		"commit %s changes the task %q from its base %s: %s%s; a candidate must not change anything under %s/%s, so restore those files from the base",
		commit, scope.Task(), head, strings.Join(named, ", "), more, task.TasksDir, scope.Task())}
}

// planFlow makes the plan of a queued check or measure job that is about to start: the rule for
// what the cache lets it skip, applied now and not when the job was queued (flow.Prepare). It
// fails when the task or the environment is not what the job was keyed under.
func (a *App) planFlow(ctx context.Context, st store.Store, scope core.Scope, job core.Job, fingerprint core.Fingerprint) (flow.Plan, error) {
	tk, err := a.taskAt(ctx, string(scope.Repository()), job.Spec.Commit, string(scope.Task()))
	if err != nil {
		return flow.Plan{}, err
	}
	var saved []core.Result
	err = a.retry(ctx, func(ctx context.Context) error {
		var err error
		saved, err = st.Results(ctx, core.ResultFilter{Commit: job.Spec.Commit})
		return err
	})
	if err != nil {
		return flow.Plan{}, storeError{err}
	}
	return flow.Prepare(flow.Starting{Job: job, NoCache: job.NoCache, Task: tk, Environment: fingerprint, Saved: saved})
}

// taskAt reads the task as the commit has it. A daemon built without a source of tasks cannot run
// a check or a measure job, and says so instead of guessing at the task.
func (a *App) taskAt(ctx context.Context, repository string, commit core.Commit, name string) (task.Task, error) {
	if a.cfg.Tasks == nil {
		return task.Task{}, &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon has no source of tasks, so it cannot run check or measure jobs, nor a command job that leaves its environment to the task"}
	}
	return a.cfg.Tasks.Task(ctx, repository, commit, name)
}

// eligibleOf is the placements a job of the kind may run on, by name, from the placement it is
// restricted to and what its task needs (core.EligiblePlacements), and never empty. Only a docker
// job runs on one: a host job runs on this machine, whatever it is restricted to, which is the
// placement "". A daemon configured with no placements, as a test's may be, leaves the restriction
// as it is, and the environment refuses a placement it does not have.
func (a *App) eligibleOf(kind core.EnvironmentKind, needs []core.ResourceKind, restriction string) ([]string, error) {
	if kind != core.EnvironmentDocker {
		return []string{""}, nil
	}
	if len(a.cfg.Placements) == 0 {
		return []string{restriction}, nil
	}
	return core.EligiblePlacements(a.cfg.Placements, needs, restriction)
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
