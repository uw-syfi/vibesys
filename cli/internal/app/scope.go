package app

import (
	"context"
	"fmt"
	"path/filepath"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// Caller is a call's context as the caller sent it, before the daemon resolved it.
type Caller struct {
	// Directory is the absolute path the command ran in, or the one it was given.
	Directory string
	Task      string
	Run       core.RunID
	Harness   string
	Agent     string
}

// Resolve turns what a caller sent into the context of its call: the task name is parsed, and the
// directory is resolved, through the common git directory, to the repository whose store the call
// is about. It is the only way a request reaches a store, so two worktrees of one repository reach
// one store. It fails with a failure of kind invalid input that names the field.
func (a *App) Resolve(ctx context.Context, c Caller) (core.CallContext, error) {
	task, err := core.ParseTaskName("context.task", c.Task)
	if err != nil {
		return core.CallContext{}, err
	}
	if !filepath.IsAbs(c.Directory) {
		return core.CallContext{}, invalid("context.repository: %q is not an absolute path", c.Directory)
	}
	if a.cfg.Repositories == nil {
		return core.CallContext{}, &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon was built without a way to resolve repositories"}
	}
	repo, err := a.cfg.Repositories.Resolve(ctx, filepath.Clean(c.Directory))
	if err != nil {
		return core.CallContext{}, err
	}
	scope, err := core.ResolvedScope(repo, task)
	if err != nil {
		return core.CallContext{}, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("resolving %s: %v", c.Directory, err)}
	}
	return core.CallContext{Scope: scope, Run: c.Run, Harness: c.Harness, Agent: c.Agent}, nil
}

// scopeOfRun is the store a run says it belongs to, when its record is its own. The record was
// written from a resolved scope, so it is read back as one; a record that does not hold one is
// nobody's.
func scopeOfRun(run supervisor.Run) (core.Scope, bool) {
	l, ok := run.Started()
	if !ok || l.Ref() != run.Job() {
		return core.Scope{}, false
	}
	scope, err := core.ResolvedScope(core.Repository(l.Repository), core.TaskName(l.Task))
	return scope, err == nil
}

// resolved fails for the zero scope, which only a call that skipped Resolve can hold.
func resolved(scope core.Scope) error {
	if scope == (core.Scope{}) {
		return &core.Failure{Kind: core.FailureInternal, Message: "the call's context was not resolved"}
	}
	return nil
}
