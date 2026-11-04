package docker

import (
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
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
	staged    *Dirs // the run's directories on the engine's machine, once staged
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
	cs, err := r.containerSpec(id, img)
	if err != nil {
		return notStarted(err)
	}
	r.attempted = true
	r.container, err = p.cfg.Engine.Start(r.ctx, cs)
	if err != nil {
		return notStarted(err)
	}
	// The limit bounds the commands and their service, and not the preparation above: building
	// an image, or waiting for an allocation the container is placed in, takes as long as it
	// takes. It is armed before anything runs in the container.
	limit, stop := p.cfg.Timers.Timeout(r.ctx, r.x.Timeout)
	r.stopLimit = stop
	return environment.Execute(r.x, r.container, environment.Events{
		Limit:     limit,
		Cancelled: r.ctx.Done(),
		Pause:     func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(r.ctx, p.cfg.ProbePause) },
		Grace:     func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(r.ctx, p.cfg.Grace) },
	}, r.sink)
}

// containerSpec stages the run's directories on the engine's machine and says what the container
// is made of.
func (r *run) containerSpec(id string, img Image) (ContainerSpec, error) {
	p := r.e.p
	m := p.cfg.Machine
	local := Dirs{Checkout: r.x.Checkout.Dir, Scratch: r.x.Scratch, Artifacts: r.x.Artifacts}
	staged, err := m.Stage(r.ctx, local)
	if err != nil {
		return ContainerSpec{}, err
	}
	r.staged = &staged
	user, err := m.User(r.ctx)
	if err != nil {
		return ContainerSpec{}, err
	}
	cache, err := m.Cache(r.ctx, cacheKey(r.e.target.Task, img.ID))
	if err != nil {
		return ContainerSpec{}, err
	}
	// The user has no home in the image, and tools want one they can write to.
	env := map[string]string{"HOME": "/tmp"}
	mounts := r.mounts(staged)
	if cache != "" {
		mounts = append(mounts, Mount{Host: cache, Container: CacheDir})
		env[EnvCacheDir] = CacheDir
	}
	if staged.Artifacts != "" {
		mounts = append(mounts, Mount{Host: staged.Artifacts, Container: ArtifactsDir})
		env[EnvArtifactsDir] = ArtifactsDir
	}
	given, err := r.attachments()
	if err != nil {
		return ContainerSpec{}, err
	}
	taken := map[string]bool{}
	for _, m := range mounts {
		taken[m.Container] = true
	}
	for _, m := range given.Mounts {
		if taken[m.Container] || m.Container == r.x.Checkout.Dir {
			return ContainerSpec{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("docker: a resource of the placement %q mounts %s, where the run has a directory of its own", p.cfg.Placement, m.Container)}
		}
		mounts = append(mounts, Mount{Host: m.Host, Container: m.Container, ReadOnly: m.ReadOnly})
	}
	for _, k := range slices.Sorted(maps.Keys(given.Env)) {
		if _, own := env[k]; own {
			return ContainerSpec{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("docker: a resource of the placement %q sets %s, which the run sets itself", p.cfg.Placement, k)}
		}
		env[k] = given.Env[k]
	}
	return ContainerSpec{
		Name:     r.name,
		Image:    img.ID,
		Labels:   runLabels(p.cfg.Scope, id),
		User:     user,
		Dir:      r.x.Checkout.Dir,
		Mounts:   mounts,
		Env:      env,
		GPUs:     GPUs{Devices: given.GPUs.Devices, Runtime: given.GPUs.Runtime},
		Network:  given.Network,
		Limit:    r.x.Timeout,
		Measured: r.x.Measured,
	}, nil
}

// attachments are what the placement's resources give the container, joined into one.
func (r *run) attachments() (resource.Attachment, error) {
	var all []resource.Attachment
	for _, res := range r.e.p.cfg.Resources {
		a, err := res.Attach(r.ctx)
		if err != nil {
			return resource.Attachment{}, err
		}
		all = append(all, a)
	}
	return resource.Combine(all)
}

// mounts are the directories the commands see: the checkout, and the scratch directory when the
// caller named one, each at the caller's own path so that a path the caller hands a command means
// the same inside the container. Both are writable: the checkout holds the build, and the scratch
// directory holds what the caller reads after the command ends.
func (r *run) mounts(staged Dirs) []Mount {
	mounts := []Mount{{Host: staged.Checkout, Container: r.x.Checkout.Dir}}
	if r.x.Scratch != "" {
		mounts = append(mounts, Mount{Host: staged.Scratch, Container: r.x.Scratch})
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
	if r.staged != nil {
		local := Dirs{Checkout: r.x.Checkout.Dir, Scratch: r.x.Scratch, Artifacts: r.x.Artifacts}
		if err := r.e.p.cfg.Machine.Collect(context.WithoutCancel(r.ctx), local, *r.staged); err != nil {
			problems = append(problems, fmt.Errorf("what the command wrote was not brought back: %v", err))
		}
	}
	if err := r.sink.Err(); err != nil {
		problems = append(problems, fmt.Errorf("the output could not be kept: %v", err))
	}
	if len(problems) == 0 {
		return nil
	}
	return &core.Failure{Kind: core.FailureInternal, Message: "docker: " + errors.Join(problems...).Error()}
}
