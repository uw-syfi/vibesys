package app

import (
	"cmp"
	"context"
	"errors"
	"path"
	"slices"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// MaxSetupJobs is the most jobs one reading of the setup facts holds: the newest ones.
const MaxSetupJobs = 200

// MaxSetupMeasurements is the most measurements one reading holds. They are of one commit under
// one task version, so more than a few means several environments; the ones with most samples stay.
const MaxSetupMeasurements = 8

// Relation is how a job's commit stands to the base of a setup reading.
type Relation int

// The relations. RelationUnknown is a commit whose object the repository no longer has.
const (
	RelationBase Relation = iota + 1
	RelationDescendant
	RelationAncestor
	RelationUnrelated
	RelationUnknown
)

// SetupJob is a check or measure job with how its commit stands to the base.
type SetupJob struct {
	Job      core.Job
	Relation Relation
	// CodeChanged is set only for a descendant: the change from the base to the job's commit
	// touches a path outside the task's directory, so it is a change of the code and not of the task.
	CodeChanged bool
}

// SetupRead is one consistent reading of a draft task for its setup checks. The daemon is the only
// reader of all three of the working tree, git and the store, and it decides nothing.
type SetupRead struct {
	// Unreadable is why the draft has no facts yet: a task or manifest that is not there, or a file
	// that is invalid. The rest is empty then, and no git or store was read.
	Unreadable *core.Failure
	// Draft is the task as the working tree has it.
	Draft task.Draft
	// Base is the commit the repository's checkout is at, which a run starting now begins from.
	Base core.Commit
	// BaseVersion is the version of the task as committed at Base, empty when it is not committed
	// there or cannot be read there.
	BaseVersion string
	// Jobs are the check and measure jobs of the task, newest submitted first, at most MaxSetupJobs.
	Jobs          []SetupJob
	JobsTruncated bool
	// Measurements are the pooled measurements of Base under Draft.Version, at most
	// MaxSetupMeasurements, the ones with most samples first.
	Measurements []core.Measurement
}

// ReadSetup reads the facts the setup checks of a draft task are derived from, in the order that
// keeps them consistent: git first (the checkout's head is the base), then the working tree's
// files, then one read of the store, and the relation of each job's commit to the base last, which
// is a fact of commits that never change. A job newer than the head read shows on the next reading.
//
// A draft that cannot be read answers Unreadable and reads nothing else. A repository with no
// commit fails as not found: there is no base to read.
func (a *App) ReadSetup(ctx context.Context, c Caller, files task.CardFiles) (SetupRead, error) {
	if files == nil {
		return SetupRead{}, &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon has no reader of task files"}
	}
	if a.cfg.Branches == nil {
		return SetupRead{}, &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon reads no branches, so it cannot read the head of a repository"}
	}
	cc, err := a.Resolve(ctx, c)
	if err != nil {
		return SetupRead{}, err
	}
	scope := cc.Scope
	repository, name := string(scope.Repository()), string(scope.Task())

	base, err := a.cfg.Branches.CheckoutHead(ctx, repository)
	if err != nil {
		return SetupRead{}, err
	}
	out := SetupRead{Base: base}
	tree, err := files.WorkingTask(ctx, repository, name)
	if err == nil {
		out.Draft, err = task.DraftOf(tree, name)
	}
	if err != nil {
		f := core.AsFailure(err)
		if f.Kind == core.FailureNotFound || f.Kind == core.FailureInvalidInput {
			return SetupRead{Unreadable: &f}, nil
		}
		return SetupRead{}, err
	}
	if out.BaseVersion, err = a.versionAt(ctx, repository, base, name); err != nil {
		return SetupRead{}, err
	}

	jobs, results, err := a.setupStore(ctx, scope)
	if err != nil {
		return SetupRead{}, err
	}
	out.Measurements = baseMeasurements(results, base, out.Draft.Version)
	jobs = slices.DeleteFunc(jobs, func(j core.Job) bool {
		return (j.Spec.Settings.Kind() != core.JobCheck && j.Spec.Settings.Kind() != core.JobMeasure) || !j.Spec.Commit.Valid()
	})
	slices.SortStableFunc(jobs, func(x, y core.Job) int {
		return cmp.Or(y.SubmittedAt.Compare(x.SubmittedAt), cmp.Compare(y.ID, x.ID))
	})
	if len(jobs) > MaxSetupJobs {
		jobs, out.JobsTruncated = jobs[:MaxSetupJobs], true
	}
	relations := map[core.Commit]SetupJob{}
	for _, j := range jobs {
		known, ok := relations[j.Spec.Commit]
		if !ok {
			if known, err = a.relationOf(ctx, repository, name, base, j.Spec.Commit); err != nil {
				return SetupRead{}, err
			}
			relations[j.Spec.Commit] = known
		}
		known.Job = j
		out.Jobs = append(out.Jobs, known)
	}
	return out, nil
}

// versionAt is the version of the task as the commit has it, empty for a commit that does not have
// it, or whose task this daemon cannot read (a task it cannot read is one no job ran at).
func (a *App) versionAt(ctx context.Context, repository string, commit core.Commit, name string) (string, error) {
	if a.cfg.Tasks == nil {
		return "", nil
	}
	t, err := a.cfg.Tasks.Task(ctx, repository, commit, name)
	if err != nil {
		if cerr := ctx.Err(); cerr != nil {
			return "", cerr
		}
		return "", nil
	}
	return t.Version, nil
}

// setupStore reads the jobs and the results of the task from one revision of its store. A task with
// no store has neither.
func (a *App) setupStore(ctx context.Context, scope core.Scope) ([]core.Job, []core.Result, error) {
	st, err := a.open(ctx, scope)
	if errors.Is(err, store.ErrNoStore) {
		return nil, nil, nil
	}
	if err != nil {
		return nil, nil, err
	}
	var snap store.TaskSnapshot
	err = a.retry(ctx, func(ctx context.Context) error {
		var err error
		snap, err = st.Snapshot(ctx)
		return err
	})
	return snap.Jobs, snap.Results, err
}

// baseMeasurements pools the results of the commit under the task version.
func baseMeasurements(results []core.Result, base core.Commit, version string) []core.Measurement {
	var of []core.Result
	for _, r := range results {
		if r.Commit == base && r.TaskVersion == version {
			of = append(of, r)
		}
	}
	pooled := core.PoolMeasurements(of)
	slices.SortStableFunc(pooled, func(x, y core.Measurement) int {
		return cmp.Compare(len(y.Outcome.Samples), len(x.Outcome.Samples))
	})
	return pooled[:min(len(pooled), MaxSetupMeasurements)]
}

// relationOf says how the commit stands to the base, and whether a descendant changes anything
// outside the task's directory. A commit the repository lacks is unknown.
func (a *App) relationOf(ctx context.Context, repository, name string, base, commit core.Commit) (SetupJob, error) {
	if commit == base {
		return SetupJob{Relation: RelationBase}, nil
	}
	branches := a.cfg.Branches
	descendant, err := branches.IsAncestor(ctx, repository, base, commit)
	if missing(err) {
		return SetupJob{Relation: RelationUnknown}, nil
	}
	if err != nil {
		return SetupJob{}, err
	}
	if descendant {
		changed, err := branches.ChangedPaths(ctx, repository, base, commit)
		if missing(err) {
			return SetupJob{Relation: RelationUnknown}, nil
		}
		if err != nil {
			return SetupJob{}, err
		}
		dir := path.Join(task.TasksDir, name) + "/"
		return SetupJob{Relation: RelationDescendant, CodeChanged: slices.ContainsFunc(changed, func(p string) bool { return !strings.HasPrefix(p, dir) })}, nil
	}
	ancestor, err := branches.IsAncestor(ctx, repository, commit, base)
	if missing(err) {
		return SetupJob{Relation: RelationUnknown}, nil
	}
	if err != nil {
		return SetupJob{}, err
	}
	if ancestor {
		return SetupJob{Relation: RelationAncestor}, nil
	}
	return SetupJob{Relation: RelationUnrelated}, nil
}

// missing says whether the error is the repository lacking a commit.
func missing(err error) bool {
	var f *core.Failure
	return errors.As(err, &f) && f.Kind == core.FailureNotFound
}
