package app

import (
	"cmp"
	"context"
	"errors"
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// foldTries bounds how often a look at a task is repeated because the store changed while it was
// read. A look that does not settle is skipped: the change that disturbed it woke the watch again.
const foldTries = 3

// Epoch names this boot of the daemon.
func (a *App) Epoch() string { return a.epoch }

// WatchTask passes to emit what a stream has to say about a task, now and every time it changes,
// until ctx is done or emit fails. Each wake looks at the task again, and core.PlanSend decides
// from that look and from what was last passed what to say; nothing here depends on how the task
// changed, so a commit that failed after landing, a recovery, a forget and a store that came back
// all end in the same fact. The wake-up is taken before the look, so a change in between wakes
// the next one; the slow period looks again when none comes.
//
// Judging covers the newest `judged` measured commits. The caller validates it.
func (a *App) WatchTask(ctx context.Context, scope core.Scope, judged int, emit func(core.Send) error) error {
	if err := resolved(scope); err != nil {
		return err
	}
	var sent core.Sent
	for {
		wake := a.tasksChanged.Wait()
		if view, ok := a.TaskView(ctx, scope, judged); ok {
			var send core.Send
			send, sent = core.PlanSend(sent, view)
			if !send.Empty() {
				if err := emit(send); err != nil {
					return err
				}
			}
		}
		if err := a.pause(ctx, wake); err != nil {
			return err
		}
	}
}

// TaskView looks at a task once, as a watch does on a wake. The second result is false when the look
// could not settle, and the caller looks again.
func (a *App) TaskView(ctx context.Context, scope core.Scope, judged int) (core.TaskView, bool) {
	for try := 0; try < foldTries; try++ {
		if ctx.Err() != nil {
			return core.TaskView{}, false
		}
		v, settled := a.lookAtTask(ctx, scope, judged)
		if settled {
			return v, true
		}
	}
	return core.TaskView{}, false
}

// lookAtTask captures all stored facts under one store lock. It serializes branch
// reads with promotion commands and rejects a fold if the store revision or the
// immutable lifecycle view changes while deriving branch and trouble facts.
func (a *App) lookAtTask(ctx context.Context, scope core.Scope, judged int) (core.TaskView, bool) {
	t, err := a.openTask(ctx, scope)
	if errors.Is(err, store.ErrNoStore) {
		// A task with no store has nothing, and nothing is the same as an empty store.
		return core.TaskView{Content: &core.TaskContent{Head: core.CurrentResultAt("", nil)}}, true
	}
	if err != nil {
		if ctx.Err() != nil {
			return core.TaskView{}, false
		}
		return awayView(err), true
	}
	a.branchMu.Lock()
	defer a.branchMu.Unlock()
	lifecycleView := a.view.Load()
	var snapshot store.TaskSnapshot
	err = a.retry(ctx, func(ctx context.Context) error {
		var err error
		snapshot, err = t.store.Snapshot(ctx)
		return err
	})
	if err != nil {
		if ctx.Err() != nil {
			return core.TaskView{}, false
		}
		return awayView(err), true
	}
	jobs, results, promotions, plans := snapshot.Jobs, snapshot.Results, snapshot.Promotions, snapshot.Plans
	c := &core.TaskContent{Plans: plans, Jobs: jobs, Results: results}
	for i, p := range promotions {
		c.Promotions = append(c.Promotions, core.PromotionRecord{Seq: uint64(i + 1), Promotion: p})
	}
	head, headed := a.headOf(ctx, t, promotions, results)
	c.Head = head
	if headed {
		c.Judgments = judgeNewest(head, results, judged)
	}
	for _, j := range jobs {
		if j.State.Final() {
			continue
		}
		if f, waits := a.waitOf(lifecycle.Ref{Store: storeIDOf(scope), Job: j.ID}); waits {
			c.Troubles = append(c.Troubles, core.JobTrouble{Job: j.ID, Failure: f})
		}
	}
	after, err := t.store.Snapshot(ctx)
	if err != nil {
		return awayView(err), true
	}
	if after.Revision != snapshot.Revision || a.view.Load() != lifecycleView {
		return core.TaskView{}, false
	}
	// Find rechecks ownership, including a store lost during the fold.
	if _, err := a.cfg.Stores.Find(ctx, scope); err != nil {
		return awayView(err), true
	}
	return core.TaskView{Content: c}, true
}

// headOf is the head of the task's result branch with its final number, as `result` reports it, and
// whether there is a head to judge against. A branch that cannot be read has no head to report.
func (a *App) headOf(ctx context.Context, t taskStore, promotions []core.Promotion, results []core.Result) (core.ResultAnswer, bool) {
	if len(promotions) == 0 {
		return core.CurrentResultAt("", nil), false
	}
	head, err := a.settledHead(ctx, t)
	if err != nil {
		var f *core.Failure
		if !errors.As(err, &f) || (f.Kind != core.FailureNotFound && f.Kind != core.FailureUnavailable) {
			a.cfg.Logf("task %s in %s: reading the result branch to report the head: %v", string(t.scope.Task()), string(t.scope.Repository()), err)
		}
		return core.CurrentResultAt("", nil), false
	}
	var at []core.Result
	for _, r := range results {
		if r.Commit == head {
			at = append(at, r)
		}
	}
	return core.CurrentResultAt(head, at), true
}

// judgeNewest judges the newest n measured commits against the head. The head is not judged
// against itself.
func judgeNewest(head core.ResultAnswer, results []core.Result, n int) []core.Judgment {
	var at core.Commit
	switch h := head.(type) {
	case core.CurrentResult:
		at = h.Head
	case core.NoCurrentResult:
		at = h.Head
	}
	if at == "" {
		return nil
	}
	var out []core.Judgment
	for _, c := range core.NewestMeasured(results, n+1) {
		if c == at {
			continue
		}
		out = append(out, core.Judge(c, at, results, noDeclaredNoise))
	}
	if len(out) > n {
		out = out[:n]
	}
	slices.SortFunc(out, func(a, b core.Judgment) int { return cmp.Compare(a.Commit, b.Commit) })
	return out
}

// awayView is the view of a store that could not be read, and why.
func awayView(err error) core.TaskView {
	f := failureOf(err)
	reason := core.AwayUnavailable
	if errors.Is(err, store.ErrOwnedElsewhere) {
		reason = core.AwayOwnedElsewhere
	}
	return core.TaskView{Health: core.StoreHealth{Away: reason, Failure: f}}
}
