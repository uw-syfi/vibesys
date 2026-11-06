package app

import (
	"context"
	"errors"
	"fmt"
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// taskStore is a task's store with the scope it was found for.
type taskStore struct {
	scope core.Scope
	store store.Store
}

// openTask finds the store of the scope and makes none: results are only ever read from a store a
// submission made.
func (a *App) openTask(ctx context.Context, scope core.Scope) (taskStore, error) {
	st, err := a.open(ctx, scope)
	return taskStore{scope: scope, store: st}, err
}

// noDeclaredNoise is the relative noise of a single-metric task in this build: such a task does
// not declare one, so its verdicts use the rule for a task that declares none. A multi-objective
// task's measurements carry their own floor, the [pareto] relative_noise, which core uses for them.
const noDeclaredNoise = 0

// Show returns every result saved for a commit, checks and measurements, oldest first, each docker
// fingerprint naming its image by content as compare sees it (withImageContents). A commit with
// none is not an error.
func (a *App) Show(ctx context.Context, scope core.Scope, commit core.Commit) ([]core.Result, error) {
	if !commit.Valid() {
		return nil, invalid("commit: %q is not a full 40-digit commit id", commit)
	}
	t, err := a.openTask(ctx, scope)
	if errors.Is(err, store.ErrNoStore) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	results, err := t.store.Results(ctx, core.ResultFilter{Commit: commit})
	if err != nil {
		return nil, err
	}
	return a.withImageContents(ctx, results), nil
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
	t, err := a.openTask(ctx, c.Scope)
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
	results, err := a.resultsOf(ctx, t, commit, against)
	if err != nil {
		return core.Comparison{}, err
	}
	return core.CompareCommits(core.CompareRequest{Commit: commit, Against: against, Results: results, RelativeNoise: noDeclaredNoise})
}

// judgeAgainstHead judges each commit against the result branch's head with the task's saved
// results, in the order given, one judgment for each distinct commit. It is the one place that
// makes judgments, and every verdict the daemon states about a commit against the head comes from
// it. A task with no readable head has none to judge against, which is not a failure: the answer
// is nil, and a head that cannot be read for a reason other than the branch not being there is noted.
func (a *App) judgeAgainstHead(ctx context.Context, t taskStore, commits []core.Commit) []core.Judgment {
	if len(commits) == 0 {
		return nil
	}
	a.branchMu.Lock()
	head, err := a.settledHead(ctx, t)
	a.branchMu.Unlock()
	if err != nil {
		var f *core.Failure
		if !errors.As(err, &f) || (f.Kind != core.FailureNotFound && f.Kind != core.FailureUnavailable) {
			a.cfg.Logf("task %s in %s: judging against the result branch: %v", string(t.scope.Task()), string(t.scope.Repository()), err)
		}
		return nil
	}
	distinct := []core.Commit{head}
	for _, c := range commits {
		if !slices.Contains(distinct, c) {
			distinct = append(distinct, c)
		}
	}
	results, err := a.resultsOf(ctx, t, distinct...)
	if err != nil {
		a.cfg.Logf("task %s in %s: reading results to judge against the result branch: %v", string(t.scope.Task()), string(t.scope.Repository()), err)
		return nil
	}
	out := make([]core.Judgment, 0, len(commits))
	var judged []core.Commit
	for _, c := range commits {
		if slices.Contains(judged, c) {
			continue
		}
		judged = append(judged, c)
		out = append(out, core.Judge(c, head, results, noDeclaredNoise))
	}
	return out
}

// verdictsOf is the verdict of each commit the jobs ran from against the result branch's head, for
// the commits that could be compared.
func (a *App) verdictsOf(ctx context.Context, t taskStore, jobs []core.Job) map[core.Commit]core.Verdict {
	var commits []core.Commit
	for _, j := range jobs {
		commits = append(commits, j.Spec.Commit)
	}
	var out map[core.Commit]core.Verdict
	for _, j := range a.judgeAgainstHead(ctx, t, commits) {
		if v := j.Verdict(); v != nil {
			if out == nil {
				out = map[core.Commit]core.Verdict{}
			}
			out[j.Commit] = *v
		}
	}
	return out
}

// Promote moves the task's result branch to the commit if core says the commit is better than the
// branch's head, and otherwise returns the refusal with every reason. Both are normal answers; a
// commit that is not a descendant of the head, a branch that is missing and a failing repository
// are failures.
//
// A multi-objective commit is better only when it is better on some objective and worse on none,
// and one that is promoted is offered to the task's frontier too: the second answer is what the
// frontier made of it, and it is nil for a refusal and for a single-metric task. The promotion
// stands whatever the frontier says.
//
// The order is the crash-safe one. The decision is made first and changes nothing. A promotion is
// then recorded in the store, and only after that is the branch fast-forwarded; core's
// NextPromotionStep names each of those two effects from what is there, and carryOut does them.
// A crash between the two leaves a promotion the branch has not caught up with, which every later
// look at the branch completes (settledHead asks the same question), so the branch ends at the
// promoted commit; a crash before the record leaves everything as it was, and the promotion can
// be asked for again. The branch is never moved ahead of the record.
func (a *App) Promote(ctx context.Context, c core.CallContext, commit core.Commit) (core.PromotionDecision, core.FrontierDecision, error) {
	if !commit.Valid() {
		return nil, nil, invalid("commit: %q is not a full 40-digit commit id", commit)
	}
	t, err := a.openTask(ctx, c.Scope)
	if err != nil {
		return nil, nil, err
	}
	branch, err := a.branchOf(t)
	if err != nil {
		return nil, nil, err
	}

	// One promotion at a time: the decision rests on the head and the record, which another
	// promotion changes.
	a.branchMu.Lock()
	defer a.branchMu.Unlock()
	head, err := a.settledHead(ctx, t)
	if err != nil {
		return nil, nil, err
	}
	results, err := a.resultsOf(ctx, t, commit, head)
	if err != nil {
		return nil, nil, err
	}
	promotions, err := t.store.Promotions(ctx)
	if err != nil {
		return nil, nil, err
	}
	decision, err := core.DecidePromotion(core.PromotionRequest{
		Candidate: commit, Head: head, Results: results, Promotions: promotions, RelativeNoise: noDeclaredNoise, At: a.now(),
	})
	if err != nil {
		return nil, nil, err
	}
	approved, promoted := decision.(core.Approved)
	if !promoted {
		return decision, nil, nil
	}

	// A fast-forward is the only move the branch makes, so a commit that does not contain the head
	// cannot be promoted. This is known before anything is recorded.
	ahead, err := a.cfg.Branches.IsAncestor(ctx, string(t.scope.Repository()), head, commit)
	if err != nil {
		return nil, nil, err
	}
	if !ahead {
		return nil, nil, &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
			"commit %s does not contain the result branch head %s, so the branch cannot fast-forward to it; merge or rebase onto the head and measure it again",
			commit, head)}
	}
	// The frontier is offered the commit before the promotion is recorded. A member needs no
	// promotion, so a crash between the two leaves a frontier that is right and a promotion that
	// can be asked for again, which finds the commit a member already and changes nothing there.
	var frontier core.FrontierDecision
	if len(approved.Promotion().Objectives) > 0 {
		if frontier, err = a.offerToFrontier(ctx, t, commit, head); err != nil {
			return nil, nil, err
		}
	}
	if err := a.carryOut(ctx, t, branch, approved); err != nil {
		return nil, nil, err
	}
	return approved, frontier, nil
}

// AddToFrontier admits a commit of a multi-objective task to the task's frontier if core says no
// member beats it or matches it, evicting the members it beats and retiring those measured only
// under another task version, and otherwise returns the refusal with every reason. Both are normal
// answers. A commit measured on one metric is invalid input: such a task has a result branch and no
// frontier. The task's current version is that of the task at the result branch head, so the
// branch must exist.
func (a *App) AddToFrontier(ctx context.Context, c core.CallContext, commit core.Commit) (core.FrontierDecision, error) {
	if !commit.Valid() {
		return nil, invalid("commit: %q is not a full 40-digit commit id", commit)
	}
	t, err := a.openTask(ctx, c.Scope)
	if err != nil {
		return nil, err
	}
	// The frontier changes under branchMu, as a promotion offers it the commit it promotes.
	a.branchMu.Lock()
	defer a.branchMu.Unlock()
	head, err := a.settledHead(ctx, t)
	if err != nil {
		return nil, err
	}
	return a.offerToFrontier(ctx, t, commit, head)
}

// offerToFrontier decides whether the commit joins the task's frontier, against the members and
// their measurements as the store holds them now, under the version of the task at the result
// branch head, and keeps an admission that changes the frontier. The caller holds branchMu.
func (a *App) offerToFrontier(ctx context.Context, t taskStore, commit, head core.Commit) (core.FrontierDecision, error) {
	current, err := a.taskAt(ctx, string(t.scope.Repository()), head, string(t.scope.Task()))
	var f *core.Failure
	if errors.As(err, &f) {
		return nil, &core.Failure{Kind: f.Kind, Message: fmt.Sprintf(
			"the frontier holds the commits of the task at the result branch head %s, which cannot be read: %s", head, f.Message)}
	}
	if err != nil {
		return nil, err
	}
	frontier, err := t.store.Frontier(ctx)
	if err != nil {
		return nil, err
	}
	commits := []core.Commit{commit}
	for _, m := range frontier.Members {
		commits = append(commits, m.Commit)
	}
	results, err := a.resultsOf(ctx, t, commits...)
	if err != nil {
		return nil, err
	}
	decision, err := core.DecideFrontier(core.FrontierRequest{
		Candidate: commit, Frontier: frontier, Results: results, TaskVersion: current.Version, At: a.now(),
	})
	if err != nil {
		return nil, err
	}
	if admission, ok := decision.(core.FrontierAdmission); ok && !admission.AlreadyMember() {
		if _, err := t.store.UpdateFrontier(ctx, admission); err != nil {
			return nil, err
		}
	}
	return decision, nil
}

// Frontier returns the task's frontier, each member with its headline number per objective, in the
// order the members were admitted. A task with no store has an empty frontier.
func (a *App) Frontier(ctx context.Context, c core.CallContext) ([]core.FrontierEntry, error) {
	t, err := a.openTask(ctx, c.Scope)
	if errors.Is(err, store.ErrNoStore) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	frontier, err := t.store.Frontier(ctx)
	if err != nil {
		return nil, err
	}
	var commits []core.Commit
	for _, m := range frontier.Members {
		commits = append(commits, m.Commit)
	}
	results, err := a.resultsOf(ctx, t, commits...)
	if err != nil {
		return nil, err
	}
	return core.FrontierEntries(frontier, results), nil
}

// maxPromotionSteps bounds carryOut. A promotion takes two effects, record and move, and one more
// look to see it is done; more means the repository or the store changes under the daemon.
const maxPromotionSteps = 3

// carryOut does what core's NextPromotionStep says, one effect at a time, until the promotion is
// recorded and the branch is at the promoted commit. The caller holds branchMu. Nothing here
// decides: it reads the head and the record, asks, and does the one effect named.
//
// Once the promotion is recorded, a failure leaves a promotion the branch has not caught up with,
// and says so, since the next look at the branch completes it.
func (a *App) carryOut(ctx context.Context, t taskStore, branch string, approved core.Approved) error {
	p := approved.Promotion()
	recorded := false
	fail := func(err error) error {
		if recorded {
			return unmoved(p.Commit, branch, err)
		}
		return err
	}
	for range maxPromotionSteps {
		head, err := a.cfg.Branches.Head(ctx, string(t.scope.Repository()), branch)
		if err != nil {
			return fail(err)
		}
		promotions, err := t.store.Promotions(ctx)
		if err != nil {
			return fail(err)
		}
		switch step := core.NextPromotionStep(p, head, promotions).(type) {
		case core.RecordPromotionStep:
			if err := t.store.RecordPromotion(ctx, approved); err != nil {
				return err
			}
			recorded = true
		case core.MoveBranchStep:
			recorded = true
			err := a.cfg.Branches.FastForward(ctx, string(t.scope.Repository()), branch, step.From, step.To)
			a.tasksChanged.Notify()
			if err != nil {
				return fail(err)
			}
		case core.PromotionDoneStep:
			return nil
		case core.PromotionDivergedStep:
			return fail(&core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
				"the result branch %s is at %s, which is neither the head %s this promotion replaces nor the promoted commit %s; someone moved it",
				branch, step.Head, p.PreviousHead, p.Commit)})
		}
	}
	return fail(&core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf(
		"the promotion of %s did not settle after %d steps", p.Commit, maxPromotionSteps)})
}

// CurrentResult reports the head of the result branch and a final number for it, or says why
// there is none. The number is the head's latest measurement that rests on a passing check of the
// same commit, task version and environment, whenever it was taken and whatever moved the branch
// to the head (core.CurrentResultAt).
func (a *App) CurrentResult(ctx context.Context, c core.CallContext) (core.ResultAnswer, error) {
	t, err := a.openTask(ctx, c.Scope)
	if errors.Is(err, store.ErrNoStore) {
		// A task with no store has promoted nothing.
		return core.CurrentResultAt("", nil), nil
	}
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
		return core.CurrentResultAt("", nil), nil
	}
	head, err := a.settledHead(ctx, t)
	if err != nil {
		return nil, err
	}
	results, err := a.resultsOf(ctx, t, head)
	if err != nil {
		return nil, err
	}
	return core.CurrentResultAt(head, results), nil
}

// resultsOf returns the saved results of the commits, with the image IDs of earlier versions'
// fingerprints resolved to the content of their images where the images are still there
// (withImageContents), so that they compare with results that pin the content.
func (a *App) resultsOf(ctx context.Context, t taskStore, commits ...core.Commit) ([]core.Result, error) {
	var out []core.Result
	var seen []core.Commit
	for _, commit := range commits {
		if slices.Contains(seen, commit) {
			continue
		}
		seen = append(seen, commit)
		rs, err := t.store.Results(ctx, core.ResultFilter{Commit: commit})
		if err != nil {
			return nil, err
		}
		out = append(out, rs...)
	}
	return a.withImageContents(ctx, out), nil
}

// branchOf names the task's result branch, or fails as unavailable when the daemon has no
// repository to ask.
func (a *App) branchOf(t taskStore) (string, error) {
	if a.cfg.Branches == nil {
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon cannot read the result branch"}
	}
	return core.ResultBranch(string(t.scope.Task()))
}

// settledHead returns the commit the result branch is at, after completing a promotion that was
// recorded and not yet applied. The caller holds branchMu.
func (a *App) settledHead(ctx context.Context, t taskStore) (core.Commit, error) {
	branch, err := a.branchOf(t)
	if err != nil {
		return "", err
	}
	head, err := a.cfg.Branches.Head(ctx, string(t.scope.Repository()), branch)
	var f *core.Failure
	if errors.As(err, &f) && f.Kind == core.FailureNotFound {
		return "", &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf(
			"the result branch %s does not exist in %s; create it at the commit the run starts from, with git branch %s <commit>",
			branch, string(t.scope.Repository()), branch)}
	}
	if err != nil {
		return "", err
	}
	promotions, err := t.store.Promotions(ctx)
	if err != nil {
		return "", err
	}
	if len(promotions) == 0 {
		return head, nil
	}
	last := promotions[len(promotions)-1]
	move, pending := core.NextPromotionStep(last, head, promotions).(core.MoveBranchStep)
	if !pending {
		return head, nil // done, or moved by someone else, and then it is theirs
	}
	err = a.cfg.Branches.FastForward(ctx, string(t.scope.Repository()), branch, move.From, move.To)
	a.tasksChanged.Notify()
	if err != nil {
		return "", err
	}
	return move.To, nil
}

// settleBranches completes the promotions that were interrupted before the daemon stopped, for
// every task it knows. A task that cannot be settled now is settled by the first call that looks
// at its branch, so a failure here is only noted.
func (a *App) settleBranches(ctx context.Context, scopes []core.Scope) {
	if a.cfg.Branches == nil {
		return
	}
	for _, scope := range scopes {
		t, err := a.openTask(ctx, scope)
		if err != nil {
			continue
		}
		a.branchMu.Lock()
		_, err = a.settledHead(ctx, t)
		a.branchMu.Unlock()
		var f *core.Failure
		if err != nil && !(errors.As(err, &f) && f.Kind == core.FailureNotFound) {
			a.cfg.Logf("task %s in %s: completing a promotion: %v", string(scope.Task()), string(scope.Repository()), err)
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
