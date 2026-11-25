package app

import (
	"context"
	"errors"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Superseded returns, of the jobs given, those core.Superseded flags against the task's result
// branch head as it is now, each with that head: queued or running measures of commits that have
// diverged from the head, which promote would refuse. It changes nothing but a promotion that was
// recorded and not yet applied, which every look at the branch completes.
//
// It is advice for a listing, so it never fails: a task with no store or no readable branch flags
// nothing, a commit the repository lacks is not flagged, and any other failure to read the
// repository is logged and flags nothing more.
func (a *App) Superseded(ctx context.Context, scope core.Scope, jobs []core.Job) map[core.JobID]core.Commit {
	var active []core.Job
	for _, j := range jobs {
		if core.Supersedable(j) {
			active = append(active, j)
		}
	}
	if len(active) == 0 || a.cfg.Branches == nil {
		return nil
	}
	t, err := a.openTask(ctx, scope)
	if err != nil {
		return nil
	}
	a.branchMu.Lock()
	head, err := a.settledHead(ctx, t)
	a.branchMu.Unlock()
	if err != nil {
		a.noteBranchTrouble(t, "reading the result branch to flag superseded measures", err)
		return nil
	}
	repository := string(t.scope.Repository())
	relations := map[core.Commit]core.HeadRelation{}
	var out map[core.JobID]core.Commit
	for _, j := range active {
		c := j.Spec.Commit
		relation, known := relations[c]
		if !known {
			relation, err = a.relationToHead(ctx, repository, head, c)
			if missing(err) {
				continue // a commit the repository lacks cannot be judged; it is not flagged
			}
			if err != nil {
				a.noteBranchTrouble(t, "comparing a measured commit with the result branch head", err)
				return out
			}
			relations[c] = relation
		}
		if core.Superseded(j, relation) {
			if out == nil {
				out = map[core.JobID]core.Commit{}
			}
			out[j.ID] = head
		}
	}
	return out
}

// relationToHead asks the repository how the commit stands to the head, with the ancestry promote
// asks before it moves the branch.
func (a *App) relationToHead(ctx context.Context, repository string, head, commit core.Commit) (core.HeadRelation, error) {
	containsHead, err := a.cfg.Branches.IsAncestor(ctx, repository, head, commit)
	if err != nil || containsHead {
		return core.RelationToHead(containsHead, false), err
	}
	inHead, err := a.cfg.Branches.IsAncestor(ctx, repository, commit, head)
	return core.RelationToHead(false, inHead), err
}

// noteBranchTrouble logs a failure to read the repository, unless it is the branch being missing
// or no repository to ask, which are ordinary.
func (a *App) noteBranchTrouble(t taskStore, doing string, err error) {
	var f *core.Failure
	if errors.As(err, &f) && (f.Kind == core.FailureNotFound || f.Kind == core.FailureUnavailable) {
		return
	}
	a.cfg.Logf("task %s in %s: %s: %v", string(t.scope.Task()), string(t.scope.Repository()), doing, err)
}
