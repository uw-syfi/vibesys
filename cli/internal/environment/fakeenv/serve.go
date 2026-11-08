package fakeenv

import (
	"context"
	"io"
	"slices"
	"sync"
	"sync/atomic"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// Serve implements environment.Environment: the server is a script, a process group of its own on
// a fake machine that the commands run against share, so that a command's `probe-ready` sees the
// server's `ready`. It is told CandidatePort as its port. As the docker environment, the fake runs
// a server only as a docker environment: of any other kind it refuses, as the host does.
func (e *Environment) Serve(ctx context.Context, s core.JobService, out io.Writer) (environment.Service, core.Attempt) {
	e.mu.Lock()
	e.served = append(e.served, s)
	e.mu.Unlock()
	if err := environment.ValidateService(s); err != nil {
		return nil, core.NotStarted{Failure: core.AsFailure(err)}
	}
	if e.kind != core.EnvironmentDocker {
		return nil, environment.NoService(e.kind)
	}
	if ctx.Err() != nil {
		return nil, core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	if out == nil {
		out = io.Discard
	}
	sv := &service{e: e, out: &syncWriter{w: out}, tail: &environment.Tail{}, ready: &atomic.Bool{}}
	sv.l = &launcher{in: &interpreter{gates: e.gates, dir: s.Checkout.Dir, files: e.files, ready: sv.ready}, wg: &sv.wg}
	startup, stopStartup := e.timers.Timeout(ctx, s.StartupTimeout)
	defer stopStartup()
	ev := environment.Events{
		Limit:     startup,
		Cancelled: ctx.Done(),
		Pause:     func() (<-chan struct{}, func()) { return e.timers.Timeout(ctx, e.pause) },
		Grace:     func() (<-chan struct{}, func()) { return e.timers.Timeout(ctx, environment.DefaultGrace) },
	}
	spec := core.ServiceSpec{Argv: environment.ResolvePort(s.Argv, CandidatePort), Probe: []string{ProbeReady}}
	proc, ended := environment.StartService(spec, environment.Places{Command: sv.l, Service: environment.Teeing(sv.l, sv.tail)}, ev, sv.out)
	if ended != nil {
		sv.l.end()
		return nil, environment.WithServiceLog(environment.Finish(ctx, ended, nil), sv.tail)
	}
	sv.w = environment.Watch(proc)
	e.mu.Lock()
	e.live = append(e.live, sv)
	e.mu.Unlock()
	return sv, nil
}

// Served returns every job-scoped server Serve was asked for, in order.
func (e *Environment) Served() []core.JobService {
	e.mu.Lock()
	defer e.mu.Unlock()
	return slices.Clone(e.served)
}

// LiveServices returns how many servers Serve started that were not closed. A test that finishes
// with one live has leaked a server.
func (e *Environment) LiveServices() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.live)
}

// CrashServices ends the process of every server that runs, as a server that crashes ends, and
// returns once each one's Alive says so. Each is still the caller's to Close.
func (e *Environment) CrashServices() {
	e.mu.Lock()
	live := slices.Clone(e.live)
	e.mu.Unlock()
	for _, s := range live {
		s.l.end()
		<-s.w.Gone()
	}
}

// service is a server Serve started: environment.Service.
type service struct {
	e     *Environment
	out   io.Writer
	tail  *environment.Tail
	ready *atomic.Bool // the server's machine answers its readiness path
	l     *launcher
	wg    sync.WaitGroup
	w     *environment.Watched

	close sync.Once
}

var _ environment.Service = (*service)(nil)

// Run implements environment.Service: the command is a script on the server's machine, in a group
// of its own that ends with the run.
func (s *service) Run(ctx context.Context, x core.Execution, out io.Writer) core.Attempt {
	e := s.e
	defer e.track(&x)()
	if err := environment.ValidateAgainst(x); err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	if ctx.Err() != nil {
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	if !s.Alive() {
		return core.NotStarted{Failure: environment.ServiceEnded()}
	}
	if out == nil {
		out = io.Discard
	}
	limit, stop := e.timers.Timeout(ctx, x.Timeout)
	defer stop()
	var wg sync.WaitGroup
	l := &launcher{in: &interpreter{gates: e.gates, dir: x.Checkout.Dir, files: e.files, ready: s.ready}, wg: &wg}
	ev := environment.Events{
		Limit:       limit,
		Cancelled:   ctx.Done(),
		Pause:       func() (<-chan struct{}, func()) { return e.timers.Timeout(ctx, e.pause) },
		Grace:       func() (<-chan struct{}, func()) { return e.timers.Timeout(ctx, environment.DefaultGrace) },
		ServiceGone: s.w.Gone(),
	}
	attempt := environment.Execute(x, l, ev, &syncWriter{w: out})
	l.end()
	return environment.WithServiceLog(environment.Finish(ctx, attempt, nil), s.tail)
}

// Alive implements environment.Service.
func (s *service) Alive() bool { return s.w.Alive() }

// Close implements environment.Service: the server is asked to terminate, given the grace period,
// and its group ended.
func (s *service) Close() error {
	s.close.Do(func() {
		ctx := context.Background()
		s.w.Stop(environment.Events{Grace: func() (<-chan struct{}, func()) { return s.e.timers.Timeout(ctx, environment.DefaultGrace) }})
		s.l.end()
		s.e.mu.Lock()
		s.e.live = slices.DeleteFunc(s.e.live, func(o *service) bool { return o == s })
		s.e.mu.Unlock()
	})
	return nil
}
