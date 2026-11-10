package docker

import (
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"slices"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// Run implements environment.Environment: it runs the execution in a container of the task's
// image, or of the candidate's, and removes the container on every path. An execution whose
// candidate serves runs in two containers, the candidate's server in one of the candidate's image
// and the command in one of the task's beside it, and both are removed on every path.
//
// The steps, in order, are: read what the task asks for and refuse what a container cannot give,
// remove the containers earlier runs left, find or build the image, arm the limit, start the
// containers, and let environment.Execute decide how the commands end. The containers are removed
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
	r := &run{e: e, ctx: ctx, x: x, sink: environment.NewSink(out), tail: &environment.Tail{}}
	attempt := r.prepareAndExecute()
	// A container that was not removed, or output that was not kept, makes the end of a command
	// that ran one the environment cannot vouch for. Before any command ran, it only adds to why
	// none did.
	return environment.WithServiceLog(environment.Finish(ctx, attempt, r.end()), r.tail)
}

// box is a container of a run.
type box struct {
	name      string
	claimed   bool // this run owns the name
	attempted bool // Start was called, so the container may exist
	container Container
	// check is what the container runs first, to see that what its resources give it works.
	check []string
}

// run is one call of Run: the containers it started and what it needs to end them.
type run struct {
	e    *env
	ctx  context.Context
	x    core.Execution
	sink *environment.Sink
	tail *environment.Tail // the candidate's server's last lines

	// boxes are the run's containers in the order they start: the candidate's first when it
	// serves, which the command's joins.
	boxes     []*box
	stopLimit []func()
	staged    *Dirs // the run's directories on the engine's machine, once staged
	// against is the job-scoped server the run's command runs against, for a run of a Service.
	against *served
}

// role is what a container of a run is for.
type role int

const (
	// roleOnly is the one container of a run: the command, and its service, in one image.
	roleOnly role = iota
	// roleServer is the candidate's server, beside the command's container.
	roleServer
	// roleBeside is the command's container of the task's image, beside the server's.
	roleBeside
	// roleService is the container of a job-scoped server, of the task's image: what roleOnly is,
	// but for the commands, which run beside it.
	roleService
	// roleAgainst is a command's container beside a job-scoped server's, joined to its network.
	roleAgainst
)

// prepareAndExecute makes the containers and runs the execution in them. Until a command starts
// in a container nothing of the execution ran: a container itself runs only a process that waits.
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
	cand := r.x.Candidate
	serving := cand != nil && cand.Serve != nil
	if serving {
		if err := r.canServe(); err != nil {
			return notStarted(err)
		}
		if _, err := r.claim(containerName(id) + candidateSuffix); err != nil {
			return notStarted(err)
		}
	}
	if _, err := r.claim(containerName(id)); err != nil {
		return notStarted(err)
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
	var candImg Image
	if cand != nil {
		var ok bool
		candImg, ok, err = p.cfg.Engine.Image(r.ctx, cand.Image)
		if err == nil && !ok {
			err = &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("docker: the engine has no candidate image %s; it is built before the run", cand.Image)}
		}
		if candImg, err = checked(candImg, err); err != nil {
			return notStarted(err)
		}
	}
	var port int
	switch {
	case serving:
		server, err := r.containerSpec(r.boxes[0], id, candImg, roleServer, "")
		if err != nil {
			return notStarted(err)
		}
		if err := r.start(r.boxes[0], server); err != nil {
			return notStarted(err)
		}
		// The server's port is chosen once its container is up, where it will listen: on a machine
		// whose network other work shares, a port is free only there and then.
		if port, err = r.boxes[0].container.Port(r.ctx, id); err != nil {
			return notStarted(err)
		}
		beside, err := r.containerSpec(r.boxes[1], id, img, roleBeside, environment.ServiceURL(port))
		if err != nil {
			return notStarted(err)
		}
		if err := r.start(r.boxes[1], beside); err != nil {
			return notStarted(err)
		}
	default:
		of := img
		if cand != nil {
			of = candImg
		}
		only, err := r.containerSpec(r.boxes[0], id, of, roleOnly, "")
		if err != nil {
			return notStarted(err)
		}
		if err := r.start(r.boxes[0], only); err != nil {
			return notStarted(err)
		}
	}
	// The limit bounds the commands and their service, and not the preparation above: building
	// an image, or waiting for an allocation the container is placed in, takes as long as it
	// takes. It is armed before anything runs in the containers.
	limit, stop := p.cfg.Timers.Timeout(r.ctx, r.x.Timeout)
	r.stopLimit = append(r.stopLimit, stop)
	for _, b := range r.boxes {
		if attempt := r.check(b, limit); attempt != nil {
			return attempt
		}
	}
	ev := environment.Events{
		Limit:     limit,
		Cancelled: r.ctx.Done(),
		Pause:     func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(r.ctx, p.cfg.ProbePause) },
		Grace:     func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(r.ctx, p.cfg.Grace) },
	}
	if !serving {
		return environment.Execute(r.x, r.boxes[0].container, ev, r.sink)
	}
	startup, stopStartup := p.cfg.Timers.Timeout(r.ctx, cand.Serve.StartupTimeout)
	r.stopLimit = append(r.stopLimit, stopStartup)
	ev.Startup = startup
	x := r.x
	x.Service = &core.ServiceSpec{
		Argv:  environment.ResolvePort(cand.Serve.Argv, port),
		Probe: []string{ProbeMount, ProbeCommand, environment.ServiceURL(port) + cand.Serve.Ready},
	}
	places := environment.Places{Command: r.boxes[1].container, Service: environment.Teeing(r.boxes[0].container, r.tail)}
	return environment.ExecuteBeside(x, places, ev, r.sink)
}

// start starts a container of the run.
func (r *run) start(b *box, spec ContainerSpec) error {
	b.attempted = true
	spec.Log = r.sink
	c, err := r.e.p.cfg.Engine.Start(r.ctx, spec)
	b.container = c
	return err
}

// check runs what the container's resources ask it to run first, if anything, within the limit,
// and says how the run ends when it fails: nothing of the execution ran, and the failure is what
// the check printed. It returns nil when the check passed or there is none.
func (r *run) check(b *box, limit <-chan struct{}) core.Attempt {
	if len(b.check) == 0 {
		return nil
	}
	out := &environment.Tail{}
	proc, err := b.container.Start(b.check, out)
	if err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	failed := func(why string) core.Attempt {
		return core.NotStarted{Failure: core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"docker: the placement %q gives the container %s what it cannot use: %s", r.e.p.cfg.Placement, b.name, why)}}
	}
	select {
	case st := <-proc.Exited():
		switch {
		case st.Err != nil:
			return failed(fmt.Sprintf("its check did not run to its end (%v)", st.Err))
		case st.Code != 0:
			said := strings.TrimSpace(out.String())
			if said == "" {
				said = fmt.Sprintf("its check exited with %d and said nothing", st.Code)
			}
			return failed(said)
		}
		return nil
	case <-limit:
		proc.Kill()
		<-proc.Exited()
		return failed("its check did not end within the command's limit")
	case <-r.ctx.Done():
		proc.Kill()
		<-proc.Exited()
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
}

// canServe refuses a run whose candidate's server this placement cannot start beside the command:
// one with no program to probe the server with, or whose resources choose the network, which the
// server's container must have to itself.
func (r *run) canServe() error {
	p := r.e.p
	if p.cfg.Probe == "" {
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"docker: the placement %q has no program to probe a candidate's server with, so it runs no task of two images: it needs a static Linux build of vibesys-cli, as a release is (CGO_ENABLED=0)", p.cfg.Placement)}
	}
	for _, as := range []role{roleServer, roleBeside} {
		given, err := r.attachments(as)
		if err != nil {
			return err
		}
		if given.Network != "" {
			return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
				"docker: a resource of the placement %q sets the network mode %q, and a task of two images runs its candidate's server in a network of its own, which the task's container joins", p.cfg.Placement, given.Network)}
		}
	}
	return nil
}

// claim takes a container name for the run.
func (r *run) claim(name string) (*box, error) {
	b := &box{name: name}
	if b.claimed = r.e.p.claim(name); !b.claimed {
		return nil, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: the container name %q is already in use", name)}
	}
	r.boxes = append(r.boxes, b)
	return b, nil
}

// containerSpec stages the run's directories on the engine's machine, once, and says what a
// container of the run is made of. url is where the command's container reaches the candidate's
// server, for roleBeside.
func (r *run) containerSpec(b *box, id string, img Image, as role, url string) (ContainerSpec, error) {
	p := r.e.p
	m := p.cfg.Machine
	if r.staged == nil {
		local := Dirs{Checkout: r.x.Checkout.Dir, Scratch: r.x.Scratch, Artifacts: r.x.Artifacts}
		staged, err := m.Stage(r.ctx, local)
		if err != nil {
			return ContainerSpec{}, err
		}
		r.staged = &staged
	}
	staged := *r.staged
	user, err := m.User(r.ctx)
	if err != nil {
		return ContainerSpec{}, err
	}
	cache, err := m.Cache(r.ctx, cacheKey(r.e.target.Task, img.ID))
	if err != nil {
		return ContainerSpec{}, err
	}
	job := &cliv1.JobEnvironment{TimeoutSeconds: environment.TimeoutSeconds(r.x.Timeout)}
	mounts := r.mounts(staged)
	if cache != "" {
		mounts = append(mounts, Mount{Host: cache, Container: CacheDir})
		job.Cache = CacheDir
	}
	if staged.Artifacts != "" {
		mounts = append(mounts, Mount{Host: staged.Artifacts, Container: ArtifactsDir})
		job.Artifacts = ArtifactsDir
	}
	if as == roleBeside || as == roleService {
		// The probe runs where the server is reached from: beside a candidate's server, and in a
		// job-scoped server's own container, whose network its commands join.
		probe, err := m.Program(r.ctx, p.cfg.Probe)
		if err != nil {
			return ContainerSpec{}, err
		}
		mounts = append(mounts, Mount{Host: probe, Container: ProbeMount, ReadOnly: true})
	}
	if as == roleBeside || as == roleAgainst {
		job.ServiceUrl = url
	}
	jobVariables, err := environment.JobVariables(job)
	if err != nil {
		return ContainerSpec{}, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: the job environment is not valid: %v", err)}
	}
	// The user has no home in the image, and tools want one they can write to.
	env := map[string]string{"HOME": "/tmp"}
	maps.Copy(env, jobVariables.Set)
	given, err := r.attachments(as)
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
	gpus := GPUs{Devices: given.GPUs.Devices, Runtime: given.GPUs.Runtime, ROCm: given.GPUs.ROCm}
	b.check = given.Check
	given.Env = maps.Clone(given.Env)
	maps.Copy(given.Env, given.GPUEnv)
	for _, k := range slices.Sorted(maps.Keys(given.Env)) {
		if _, own := env[k]; own {
			return ContainerSpec{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("docker: a resource of the placement %q sets %s, which the run sets itself", p.cfg.Placement, k)}
		}
		env[k] = given.Env[k]
	}
	cs := ContainerSpec{
		Name:     b.name,
		Image:    img.ID,
		Labels:   runLabels(p.cfg.Scope, id),
		User:     user,
		Dir:      r.x.Checkout.Dir,
		Mounts:   mounts,
		Env:      env,
		GPUs:     gpus,
		Network:  given.Network,
		Limit:    r.x.Timeout,
		Measured: r.x.Measured,
	}
	// A job that runs inside a standing allocation names it on its first container, which the
	// placement's engine reads (Config.AllocationLabel) to run the container inside it.
	if key := p.cfg.AllocationLabel; key != "" && r.e.target.Allocation != "" && b == r.boxes[0] {
		cs.Labels[key] = r.e.target.Allocation
	}
	switch as {
	case roleBeside:
		cs.Network = NetworkOf(r.boxes[0].name)
	case roleAgainst:
		// A network a resource asks for is the server's, which this container joins.
		cs.Network = NetworkOf(r.against.name)
	default:
		// The container has the network its resources ask for.
	}
	return cs, nil
}

// NetworkOf is the network mode of a container that joins the network of the named one.
func NetworkOf(container string) string { return "container:" + container }

// attachments are what the placement's resources give the container, joined into one.
func (r *run) attachments(as role) (resource.Attachment, error) {
	var all []resource.Attachment
	for _, res := range r.e.p.cfg.Resources {
		s := shareOf(res.Kind(), as)
		if !s.given {
			continue
		}
		a, err := res.Attach(r.ctx)
		if err != nil {
			return resource.Attachment{}, err
		}
		if s.readOnly {
			a.Mounts = slices.Clone(a.Mounts)
			for i := range a.Mounts {
				a.Mounts[i].ReadOnly = true
			}
		}
		all = append(all, a)
	}
	return resource.Combine(all)
}

// share is how a container of a run gets a resource of the placement.
type share struct {
	given    bool
	readOnly bool // its mounts are read-only, whatever the resource says
}

// twoImageShares says which of the two containers of a task of two images gets each kind of
// resource: the GPUs are the candidate server's; the mounted directories, such as model weights
// and tokenizer files, are both containers', read-only, so neither changes what the other reads;
// a cluster is the task's, whose commands drive it.
var twoImageShares = map[core.ResourceKind]map[role]share{
	core.ResourceGPU:        {roleServer: {given: true}},
	core.ResourceROCm:       {roleServer: {given: true}},
	core.ResourceMounts:     {roleServer: {given: true, readOnly: true}, roleBeside: {given: true, readOnly: true}},
	core.ResourceKubernetes: {roleBeside: {given: true}},
}

// againstShares says which resources a command's container beside a job-scoped server gets: the
// mounted directories and a cluster, as they are, and not the GPUs, which are the server's. The
// server's container gets every resource, as the only container of a task of one image does.
var againstShares = map[core.ResourceKind]share{
	core.ResourceMounts:     {given: true},
	core.ResourceKubernetes: {given: true},
}

// shareOf is how a container in the role gets a resource of the kind. The only container of a
// task of one image gets every resource as it is.
func shareOf(kind core.ResourceKind, as role) share {
	switch as {
	case roleOnly, roleService:
		return share{given: true}
	case roleAgainst:
		return againstShares[kind]
	default:
		return twoImageShares[kind][as]
	}
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

// end removes the containers and everything in them, the command's before the server's, waits for
// the engine's side of them to finish, and reports what it could not do. It runs on every path,
// and nothing the caller does stops it.
func (r *run) end() error {
	var problems []error
	for i := len(r.boxes) - 1; i >= 0; i-- {
		b := r.boxes[i]
		if b.attempted {
			// A cancelled context must not leave the container behind.
			if err := r.e.p.cfg.Engine.Remove(context.WithoutCancel(r.ctx), b.name); err != nil {
				problems = append(problems, fmt.Errorf("the container %s was not removed (%v); it stops by itself and the next run removes it", b.name, err))
			}
		}
		if b.container != nil {
			b.container.Close()
		}
		if b.claimed {
			r.e.p.release(b.name)
		}
	}
	for _, stop := range r.stopLimit {
		stop()
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
