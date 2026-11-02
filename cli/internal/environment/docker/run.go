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
func (e *env) Run(ctx context.Context, x core.Execution, out io.Writer) (core.ExecOutcome, error) {
	if err := environment.Validate(x); err != nil {
		return core.ExecOutcome{}, err
	}
	if ctx.Err() != nil {
		return core.ExecOutcome{Kind: core.ExecCancelled}, nil
	}
	if out == nil {
		out = io.Discard
	}
	r := &run{e: e, ctx: ctx, x: x, sink: environment.NewSink(out)}
	outcome, err := r.prepareAndExecute()
	lost := r.end()
	if err != nil && ctx.Err() != nil {
		// A step that failed while the caller was cancelling it was ended by the cancellation.
		outcome, err = core.ExecOutcome{Kind: core.ExecCancelled}, nil
	}
	switch {
	case err != nil && lost != nil:
		// Both matter: the run failed, and what it started may be left. The first error keeps its
		// kind for the caller.
		return core.ExecOutcome{}, errors.Join(err, lost)
	case err != nil:
		return core.ExecOutcome{}, err
	case lost != nil:
		// The command ran, but the container was not removed, or what it wrote was not kept: the
		// environment cannot say what the run left behind or whether its result is whole.
		return core.ExecOutcome{Kind: core.ExecUnknown}, lost
	}
	return outcome, nil
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

func (r *run) prepareAndExecute() (core.ExecOutcome, error) {
	p := r.e.p
	spec, err := r.e.spec(r.ctx)
	if err != nil {
		return core.ExecOutcome{}, err
	}
	id := p.cfg.IDs.New()
	if err := checkRunID(id); err != nil {
		return core.ExecOutcome{}, err
	}
	r.name = containerName(id)
	if r.claimed = p.claim(r.name); !r.claimed {
		return core.ExecOutcome{}, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: the run id %q is already in use", id)}
	}
	if err := p.sweep(r.ctx); err != nil {
		return core.ExecOutcome{}, err
	}
	// The build's output is not the command's: a run's output must not depend on whether its
	// image was already built. A failed build says why in its error.
	img, err := p.image(r.ctx, spec, io.Discard)
	if err != nil {
		return core.ExecOutcome{}, err
	}
	// The limit bounds the commands and their service, and not the preparation above: building
	// an image takes as long as it takes. It is armed before anything runs in the container.
	limit, stop := p.cfg.Timers.Timeout(r.x.Timeout)
	r.stopLimit = stop

	r.attempted = true
	r.container, err = p.cfg.Engine.Start(r.ctx, ContainerSpec{
		Name:   r.name,
		Image:  img.ID,
		Labels: runLabels(p.cfg.Scope, id),
		User:   p.cfg.User,
		Dir:    r.x.Checkout.Dir,
		Mounts: []Mount{{Host: r.x.Checkout.Dir, Container: r.x.Checkout.Dir}},
		// The user has no home in the image, and tools want one they can write to.
		Env: map[string]string{"HOME": "/tmp"},
	})
	if err != nil {
		return core.ExecOutcome{}, err
	}
	return environment.Execute(r.x, r.container, environment.Events{
		Limit:     limit,
		Cancelled: r.ctx.Done(),
		Pause:     func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(p.cfg.ProbePause) },
	}, r.sink)
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
