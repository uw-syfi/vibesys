package docker

import (
	"context"
	"errors"
	"fmt"
	"io"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// Run implements environment.Environment: it runs the execution in a container of the task's
// image and removes the container on every path.
//
// The steps, in order, are: read what the task asks for and refuse what a container cannot give,
// remove the containers earlier runs left, find or build the image, arm the limit, start the
// container, and let environment.Execute decide how the commands end. The container is removed
// whatever happened, with a context that the caller's cancellation cannot cut short.
func (e *env) Run(ctx context.Context, x core.Execution, out io.Writer) core.Attempt {
	if err := environment.Validate(x); err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	if ctx.Err() != nil {
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	if out == nil {
		out = io.Discard
	}
	r := &run{e: e, ctx: ctx, x: x, sink: environment.NewSink(out)}
	attempt := r.prepareAndExecute()
	// A container that was not removed, or output that was not kept, makes the end of a command
	// that ran one the environment cannot vouch for. Before any command ran, it only adds to why
	// none did.
	return environment.Finish(ctx, attempt, r.end())
}

// run is one call of Run: the container it started and what it needs to end it.
type run struct {
	e    *env
	ctx  context.Context
	x    core.Execution
	sink *environment.Sink

	name      string // the container's name, once it is chosen
	claimed   bool   // this run owns the name
	attempted bool   // Start was called, so the container may exist
	container Container
	stopLimit func()
}

// prepareAndExecute makes the container and runs the execution in it. Until a command starts in
// the container nothing of the execution ran: the container itself runs only a process that waits.
func (r *run) prepareAndExecute() core.Attempt {
	notStarted := func(err error) core.Attempt { return core.NotStarted{Failure: core.AsFailure(err)} }
	p := r.e.p
	spec, err := r.e.spec(r.ctx)
	if err != nil {
		return notStarted(err)
	}
	id := p.cfg.IDs.New()
	if err := checkRunID(id); err != nil {
		return notStarted(err)
	}
	r.name = containerName(id)
	if r.claimed = p.claim(r.name); !r.claimed {
		return notStarted(&core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: the run id %q is already in use", id)})
	}
	if err := p.sweep(r.ctx); err != nil {
		return notStarted(err)
	}
	// The build's output is not the command's: a run's output must not depend on whether its
	// image was already built. A failed build says why in its error.
	img, err := p.image(r.ctx, spec, io.Discard)
	if err != nil {
		return notStarted(err)
	}
	// The limit bounds the commands and their service, and not the preparation above: building
	// an image takes as long as it takes. It is armed before anything runs in the container.
	limit, stop := p.cfg.Timers.Timeout(r.ctx, r.x.Timeout)
	r.stopLimit = stop

	r.attempted = true
	r.container, err = p.cfg.Engine.Start(r.ctx, ContainerSpec{
		Name:   r.name,
		Image:  img.ID,
		Labels: runLabels(p.cfg.Scope, id),
		User:   p.cfg.User,
		Dir:    r.x.Checkout.Dir,
		Mounts: r.mounts(),
		// The user has no home in the image, and tools want one they can write to.
		Env: map[string]string{"HOME": "/tmp"},
	})
	if err != nil {
		return notStarted(err)
	}
	return environment.Execute(r.x, r.container, environment.Events{
		Limit:     limit,
		Cancelled: r.ctx.Done(),
		Pause:     func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(r.ctx, p.cfg.ProbePause) },
	}, r.sink)
}

// mounts are the directories the commands see: the checkout, and the scratch directory when the
// caller named one, each at its own path so that a path the caller hands a command means the same
// inside the container. Both are writable: the checkout holds the build, and the scratch directory
// holds what the caller reads after the command ends.
func (r *run) mounts() []Mount {
	mounts := []Mount{{Host: r.x.Checkout.Dir, Container: r.x.Checkout.Dir}}
	if r.x.Scratch != "" {
		mounts = append(mounts, Mount{Host: r.x.Scratch, Container: r.x.Scratch})
	}
	return mounts
}

// end removes the container and everything in it, waits for the engine's side of it to finish, and
// reports what it could not do. It runs on every path, and nothing the caller does stops it.
func (r *run) end() error {
	if r.claimed {
		defer r.e.p.release(r.name)
	}
	var problems []error
	if r.attempted {
		// A cancelled context must not leave the container behind.
		if err := r.e.p.cfg.Engine.Remove(context.WithoutCancel(r.ctx), r.name); err != nil {
			problems = append(problems, fmt.Errorf("the container %s was not removed (%v); it stops by itself and the next run removes it", r.name, err))
		}
	}
	if r.container != nil {
		r.container.Close()
	}
	if r.stopLimit != nil {
		r.stopLimit()
	}
	if err := r.sink.Err(); err != nil {
		problems = append(problems, fmt.Errorf("the output could not be kept: %v", err))
	}
	if len(problems) == 0 {
		return nil
	}
	return &core.Failure{Kind: core.FailureInternal, Message: "docker: " + errors.Join(problems...).Error()}
}
