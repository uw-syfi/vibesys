package docker

import (
	"context"
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// TaskSource is the Source that reads a task as the daemon does everywhere else, from the
// repository at the commit, so the image a job runs in comes from the same files its key and its
// plan come from.
type TaskSource struct {
	Tasks task.Source
}

var _ Source = TaskSource{}

// Spec implements Source. A task without a Dockerfile is refused with the way out, because the
// environment was asked for by name and nothing falls back to another one.
func (s TaskSource) Spec(ctx context.Context, target environment.Target) (Spec, error) {
	t, err := s.Tasks.Task(ctx, target.Repository, target.Commit, target.Task)
	if err != nil {
		return Spec{}, err
	}
	if !t.HasDockerfile {
		return Spec{}, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf(
			"task %q has no Dockerfile at commit %s, so it has no image to run in; run it with --env host", target.Task, target.Commit)}
	}
	return Spec{Dockerfile: t.Dockerfile, NestedDocker: t.NestedDocker}, nil
}
