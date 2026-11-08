package docker

import (
	"context"
	"fmt"
	"io"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// A task of one image may declare a job-scoped server. Its container, of the task's image, is
// started once and holds the placement's resources as the only container of a run would; it stays
// up across the job's runs, each of which starts the command in a container of its own, joined to
// the server's network, as the command of a task of two images joins its candidate's server's. On
// a cluster that joined container runs in the server's allocation, each of its processes a step of
// it. The server's container is a container of the scope that a live run of this process owns, so
// a run's sweep leaves it, and it is removed by Close, or, when the caller dies, stops by itself
// and is swept by the next run as any container is.

// serviceSuffix ends the name of a job-scoped server's container.
const serviceSuffix = "-service"

// Serve implements environment.Environment: it starts the server in a container of the task's
// image, probes it from inside that container until it is ready, and returns it running. A start
// that fails removes the container before it returns.
func (e *env) Serve(ctx context.Context, s core.JobService, out io.Writer) (environment.Service, core.Attempt) {
	if err := environment.ValidateService(s); err != nil {
		return nil, core.NotStarted{Failure: core.AsFailure(err)}
	}
	if ctx.Err() != nil {
		return nil, core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	if out == nil {
		out = io.Discard
	}
	// The server's container lives as long as the job may use it: on a cluster its allocation is
	// asked for that long, and is the whole node, since the job's samples are timed against it.
	x := core.Execution{Checkout: s.Checkout, Artifacts: s.Artifacts, Argv: s.Argv, Timeout: s.Lifetime, Measured: true}
	r := &run{e: e, ctx: ctx, x: x, sink: environment.NewSink(out), tail: &environment.Tail{}}
	sv, attempt := r.serve(s)
	if attempt != nil {
		return nil, environment.WithServiceLog(environment.Finish(ctx, attempt, r.end()), r.tail)
	}
	return sv, nil
}

// serve starts the server's container and the server in it, and waits until it is ready.
func (r *run) serve(s core.JobService) (*served, core.Attempt) {
	notStarted := func(err error) core.Attempt { return core.NotStarted{Failure: core.AsFailure(err)} }
	p := r.e.p
	if p.cfg.Probe == "" {
		return nil, notStarted(&core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"docker: the placement %q has no program to probe a server with, so it runs no task with a [service]: it needs a static Linux build of vibesys-cli, as a release is (CGO_ENABLED=0)", p.cfg.Placement)})
	}
	spec, err := r.e.spec(r.ctx)
	if err != nil {
		return nil, notStarted(err)
	}
	id := p.cfg.IDs.New()
	if err := checkRunID(id); err != nil {
		return nil, notStarted(err)
	}
	b, err := r.claim(containerName(id) + serviceSuffix)
	if err != nil {
		return nil, notStarted(err)
	}
	if err := p.sweep(r.ctx); err != nil {
		return nil, notStarted(err)
	}
	img, err := p.image(r.ctx, spec, io.Discard)
	if err != nil {
		return nil, notStarted(err)
	}
	cs, err := r.containerSpec(b, id, img, roleService, "")
	if err != nil {
		return nil, notStarted(err)
	}
	if err := r.start(b, cs); err != nil {
		return nil, notStarted(err)
	}
	port, err := b.container.Port(r.ctx, id)
	if err != nil {
		return nil, notStarted(err)
	}
	// The startup time bounds the resources' check and the wait for the server, and nothing after:
	// the server then runs until Close.
	startup, stopStartup := p.cfg.Timers.Timeout(r.ctx, s.StartupTimeout)
	defer stopStartup()
	if attempt := r.check(b, startup); attempt != nil {
		return nil, attempt
	}
	ev := environment.Events{
		Limit:     startup,
		Cancelled: r.ctx.Done(),
		Pause:     func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(r.ctx, p.cfg.ProbePause) },
		Grace:     func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(r.ctx, p.cfg.Grace) },
	}
	server := core.ServiceSpec{
		Argv:  environment.ResolvePort(s.Argv, port),
		Probe: []string{ProbeMount, ProbeCommand, environment.ServiceURL(port) + s.Ready},
	}
	places := environment.Places{Command: b.container, Service: environment.Teeing(b.container, r.tail)}
	proc, ended := environment.StartService(server, places, ev, r.sink)
	if ended != nil {
		return nil, ended
	}
	return &served{r: r, name: b.name, port: port, w: environment.Watch(proc)}, nil
}

// served is a job-scoped server that runs: environment.Service.
type served struct {
	r    *run // the server's: its container, its output and its last lines
	name string
	port int
	w    *environment.Watched

	close sync.Once
}

var _ environment.Service = (*served)(nil)

// Run implements environment.Service: the command runs in a container of its own, joined to the
// server's network, which is removed on every path; the server's is not touched.
func (s *served) Run(ctx context.Context, x core.Execution, out io.Writer) core.Attempt {
	if err := environment.ValidateAgainst(x); err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	if ctx.Err() != nil {
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	if out == nil {
		out = io.Discard
	}
	r := &run{e: s.r.e, ctx: ctx, x: x, sink: environment.NewSink(out), tail: s.r.tail, against: s}
	attempt := r.executeAgainst()
	return environment.WithServiceLog(environment.Finish(ctx, attempt, r.end()), s.r.tail)
}

// executeAgainst makes the command's container beside the server's and runs the execution in it.
func (r *run) executeAgainst() core.Attempt {
	notStarted := func(err error) core.Attempt { return core.NotStarted{Failure: core.AsFailure(err)} }
	p := r.e.p
	if !r.against.Alive() {
		return core.NotStarted{Failure: environment.ServiceEnded()}
	}
	spec, err := r.e.spec(r.ctx)
	if err != nil {
		return notStarted(err)
	}
	id := p.cfg.IDs.New()
	if err := checkRunID(id); err != nil {
		return notStarted(err)
	}
	b, err := r.claim(containerName(id))
	if err != nil {
		return notStarted(err)
	}
	if err := p.sweep(r.ctx); err != nil {
		return notStarted(err)
	}
	img, err := p.image(r.ctx, spec, io.Discard)
	if err != nil {
		return notStarted(err)
	}
	cs, err := r.containerSpec(b, id, img, roleAgainst, environment.ServiceURL(r.against.port))
	if err != nil {
		return notStarted(err)
	}
	if err := r.start(b, cs); err != nil {
		return notStarted(err)
	}
	limit, stop := p.cfg.Timers.Timeout(r.ctx, r.x.Timeout)
	r.stopLimit = append(r.stopLimit, stop)
	if attempt := r.check(b, limit); attempt != nil {
		return attempt
	}
	ev := environment.Events{
		Limit:       limit,
		Cancelled:   r.ctx.Done(),
		Pause:       func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(r.ctx, p.cfg.ProbePause) },
		Grace:       func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(r.ctx, p.cfg.Grace) },
		ServiceGone: r.against.w.Gone(),
	}
	return environment.Execute(r.x, b.container, ev, r.sink)
}

// Alive implements environment.Service.
func (s *served) Alive() bool { return s.w.Alive() }

// Close implements environment.Service: the server is asked to terminate and given the grace
// period, and its container is removed, with a context the caller's cancellation cannot cut short.
func (s *served) Close() error {
	var err error
	s.close.Do(func() {
		p := s.r.e.p
		ctx := context.WithoutCancel(s.r.ctx)
		s.w.Stop(environment.Events{Grace: func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(ctx, p.cfg.Grace) }})
		err = s.r.end()
	})
	return err
}
