package daemon

import (
	"context"
	"errors"
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// namedSource is a task.Source whose failures say which daemon read the task. A client may be
// talking to a daemon of another build than its own, and a task one build refuses another reads,
// so a refusal that does not name the daemon sends its reader to the task and not to the build.
type namedSource struct {
	task.Source
	version string
}

// named returns src with failures that carry the daemon's build. A nil source stays nil, since a
// daemon without one refuses those jobs as unavailable.
func named(src task.Source, version string) task.Source {
	if src == nil {
		return nil
	}
	return namedSource{Source: src, version: version}
}

// Task implements task.Source. Only a failure of reading the task is renamed; its kind is kept.
func (n namedSource) Task(ctx context.Context, repository string, commit core.Commit, name string) (task.Task, error) {
	t, err := n.Source.Task(ctx, repository, commit, name)
	var f *core.Failure
	if errors.As(err, &f) && f.Kind != core.FailureNotFound {
		return t, &core.Failure{Kind: f.Kind, Message: fmt.Sprintf("the vibesys-cli daemon of build %s could not read the task: %s", n.version, f.Message)}
	}
	return t, err
}
