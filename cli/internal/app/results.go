package app

import (
	"context"
	"errors"
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// noDeclaredNoise is the relative noise every task has in this build: tasks do not declare one
// yet, so verdicts use the rule for a task that declares none.
const noDeclaredNoise = 0

// Show returns every result saved for a commit, checks and measurements, oldest first. A commit
// with none is not an error.
func (a *App) Show(ctx context.Context, scope Scope, commit core.Commit) ([]core.Result, error) {
	if !commit.Valid() {
		return nil, invalid("commit: %q is not a full 40-digit commit id", commit)
	}
	t, err := a.open(ctx, scope)
	if err != nil {
		return nil, err
	}
	return t.store.Results(ctx, core.ResultFilter{Commit: commit})
}

// Compare says how a commit's saved measurements compare with another commit's, which is the
// result branch's head when against is empty. Both sides are only read: nothing it does changes
// the store or the repository.
func (a *App) Compare(ctx context.Context, c core.CallContext, commit, against core.Commit) (core.Comparison, error) {
	if !commit.Valid() {
		return core.Comparison{}, invalid("commit: %q is not a full 40-digit commit id", commit)
	}
	if against != "" && !against.Valid() {
		return core.Comparison{}, invalid("against: %q is not a full 40-digit commit id", against)
	}
	t, err := a.open(ctx, scopeOf(c))
	if err != nil {
		return core.Comparison{}, err
	}
	if against == "" {
		a.branchMu.Lock()
		against, err = a.settledHead(ctx, t)
		a.branchMu.Unlock()
		if err != nil {
			return core.Comparison{}, err
		}
	}
	results, err := resultsOf(ctx, t, commit, against)
	if err != nil {
		return core.Comparison{}, err
	}
	return core.CompareCommits(core.CompareRequest{Commit: commit, Against: against, Results: results, RelativeNoise: noDeclaredNoise})
}

// Promote moves the task's result branch to the commit if core says the commit is better than the
// branch's head, and otherwise returns the refusal with every reason. Both are normal answers; a
// commit that is not a descendant of the head, a branch that is missing and a failing repository
// are failures.
//
// The order is the crash-safe one. The decision is made first and changes nothing. A promotion is
// then recorded in the store, and only after that is the branch fast-forwarded. A crash between
// the two leaves a promotion the branch has not caught up with, which every later look at the
// branch completes (settledHead), so the branch ends at the promoted commit; a crash before the
// record leaves everything as it was, and the promotion can be asked for again. The branch is
// never moved ahead of the record.
func (a *App) Promote(ctx context.Context, c core.CallContext, commit core.Commit) (core.PromotionDecision, error) {
	if !commit.Valid() {
		return nil, invalid("commit: %q is not a full 40-digit commit id", commit)
	}
	t, err := a.open(ctx, scopeOf(c))
	if err != nil {
		return nil, err
	}
	branch, err := a.branchOf(t)
	if err != nil {
		return nil, err
	}

	// One promotion at a time: the decision rests on the head and the record, which another
	// promotion changes.
	a.branchMu.Lock()
	defer a.branchMu.Unlock()
	head, err := a.settledHead(ctx, t)
	if err != nil {
		return nil, err
	}
	results, err := resultsOf(ctx, t, commit, head)
	if err != nil {
		return nil, err
	}
	promotions, err := t.store.Promotions(ctx)
	if err != nil {
		return nil, err
	}
	decision, err := core.DecidePromotion(core.PromotionRequest{
		Candidate: commit, Head: head, Results: results, Promotions: promotions, RelativeNoise: noDeclaredNoise, At: a.now(),
	})
	if err != nil {
		return nil, err
	}
	promotion, promoted := decision.(core.Promotion)
	if !promoted {
		return decision, nil
	}

	// A fast-forward is the only move the branch makes, so a commit that does not contain the head
	// cannot be promoted. This is known before anything is recorded.
	ahead, err := a.cfg.Branches.IsAncestor(ctx, t.scope.Repository, head, commit)
	if err != nil {
		return nil, err
	}
	if !ahead {
		return nil, &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
			"commit %s does not contain the result branch head %s, so the branch cannot fast-forward to it; merge or rebase onto the head and measure it again",
			commit, head)}
	}
	if err := t.store.RecordPromotion(ctx, promotion); err != nil {
		return nil, err
	}
	if err := a.cfg.Branches.FastForward(ctx, t.scope.Repository, branch, head, commit); err != nil {
		return nil, unmoved(commit, branch, err)
	}
	return promotion, nil
}

// CurrentResult reports the head of the result branch and a final number for it, or says why
// there is none. The number is a measurement of the head with a check, both taken after the head
// last moved: the cache cannot supply it.
func (a *App) CurrentResult(ctx context.Context, c core.CallContext) (core.ResultAnswer, error) {
	t, err := a.open(ctx, scopeOf(c))
	if err != nil {
		return nil, err
	}
	a.branchMu.Lock()
	defer a.branchMu.Unlock()
	promotions, err := t.store.Promotions(ctx)
	if err != nil {
		return nil, err
	}
	if len(promotions) == 0 {
		return core.CurrentResultAt("", nil, nil), nil
	}
	head, err := a.settledHead(ctx, t)
	if err != nil {
		return nil, err
	}
	results, err := t.store.Results(ctx, core.ResultFilter{Commit: head})
	if err != nil {
		return nil, err
	}
	return core.CurrentResultAt(head, promotions, results), nil
}

// resultsOf returns the saved results of the commits.
func resultsOf(ctx context.Context, t *taskEntry, commits ...core.Commit) ([]core.Result, error) {
	var out []core.Result
	for _, commit := range commits {
		rs, err := t.store.Results(ctx, core.ResultFilter{Commit: commit})
		if err != nil {
			return nil, err
		}
		out = append(out, rs...)
	}
	return out, nil
}

// branchOf names the task's result branch, or fails as unavailable when the daemon has no
// repository to ask.
func (a *App) branchOf(t *taskEntry) (string, error) {
	if a.cfg.Branches == nil {
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon cannot read the result branch"}
	}
	return core.ResultBranch(t.scope.Task)
}

// settledHead returns the commit the result branch is at, after completing a promotion that was
// recorded and not yet applied. The caller holds branchMu.
func (a *App) settledHead(ctx context.Context, t *taskEntry) (core.Commit, error) {
	branch, err := a.branchOf(t)
	if err != nil {
		return "", err
	}
	head, err := a.cfg.Branches.Head(ctx, t.scope.Repository, branch)
	var f *core.Failure
	if errors.As(err, &f) && f.Kind == core.FailureNotFound {
		return "", &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf(
			"the result branch %s does not exist in %s; create it at the commit the run starts from, with git branch %s <commit>",
			branch, t.scope.Repository, branch)}
	}
	if err != nil {
		return "", err
	}
	promotions, err := t.store.Promotions(ctx)
	if err != nil {
		return "", err
	}
	to, pending := core.PendingMove(head, promotions)
	if !pending {
		return head, nil
	}
	behind, err := a.cfg.Branches.IsAncestor(ctx, t.scope.Repository, head, to)
	if errors.As(err, &f) && f.Kind == core.FailureNotFound {
		return head, nil // the repository lacks the promoted commit: nothing can be completed
	}
	if err != nil || !behind {
		return head, err
	}
	if err := a.cfg.Branches.FastForward(ctx, t.scope.Repository, branch, head, to); err != nil {
		return "", err
	}
	return to, nil
}

// settleBranches completes the promotions that were interrupted before the daemon stopped, for
// every task it knows. A task that cannot be settled now is settled by the first call that looks
// at its branch, so a failure here is only noted.
func (a *App) settleBranches(ctx context.Context, scopes []Scope) {
	if a.cfg.Branches == nil {
		return
	}
	for _, scope := range scopes {
		t, err := a.open(ctx, scope)
		if err != nil {
			continue
		}
		a.branchMu.Lock()
		_, err = a.settledHead(ctx, t)
		a.branchMu.Unlock()
		var f *core.Failure
		if err != nil && !(errors.As(err, &f) && f.Kind == core.FailureNotFound) {
			a.cfg.Logf("task %s in %s: completing a promotion: %v", scope.Task, scope.Repository, err)
		}
	}
}

// unmoved is the failure for a promotion that is recorded while its branch move failed. It keeps
// the kind of the failure and says what state is left.
func unmoved(commit core.Commit, branch string, cause error) error {
	f := failureOf(cause)
	f.Message = fmt.Sprintf("the promotion of %s is recorded but the branch %s has not moved yet; it moves the next time the daemon looks at the branch: %s", commit, branch, f.Message)
	return &f
}
