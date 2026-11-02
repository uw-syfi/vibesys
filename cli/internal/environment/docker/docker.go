package docker

import (
	"context"
	"fmt"
	"io"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// Config configures New. Every field but ProbePause is required.
type Config struct {
	Engine Engine
	// Source reads the Dockerfile of a task at a commit.
	Source Source
	// Backend describes the machine: what the fingerprint is made of, and which backend the
	// containers serve. Only the CPU backend is supported.
	Backend backend.Backend
	// Timers makes every timeout and pause Run waits on. The first timer a Run arms is the
	// execution's limit, with the duration of its Timeout; later ones are pauses between probes.
	Timers clock.Timers
	// IDs names the runs.
	IDs IDs
	// Scope is whose containers these are: a run removes the containers of its scope that no live
	// run of this process owns, so two live runs in different processes must not share one. A
	// daemon uses one scope (ScopeFor) for the jobs it runs one at a time.
	Scope string
	// User is the user and group the commands run as. It is the invoking user, so the files the
	// commands write are theirs.
	User User
	// ProbePause is the pause between two probes. Zero means environment.DefaultProbePause.
	ProbePause time.Duration
}

// Provider makes the docker environments of one process. They share what must be one per
// process: the record of which containers live runs own, and the lock that lets one image be
// built at a time. Create it with New.
type Provider struct {
	cfg Config

	build chan struct{} // a token held while an image is built

	mu     sync.Mutex
	active map[string]struct{} // containers of live runs, by name
}

// New returns a Provider, or a core.FailureInvalidInput that names the field that is missing or
// malformed.
func New(cfg Config) (*Provider, error) {
	missing := func(field string) error {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: "docker: the config has no " + field}
	}
	switch {
	case cfg.Engine == nil:
		return nil, missing("Engine")
	case cfg.Source == nil:
		return nil, missing("Source")
	case cfg.Backend == nil:
		return nil, missing("Backend")
	case cfg.Timers == nil:
		return nil, missing("Timers")
	case cfg.IDs == nil:
		return nil, missing("IDs")
	case cfg.User.UID < 0 || cfg.User.GID < 0:
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("docker: the user %d:%d is negative", cfg.User.UID, cfg.User.GID)}
	}
	if err := checkScope(cfg.Scope); err != nil {
		return nil, err
	}
	if cfg.ProbePause <= 0 {
		cfg.ProbePause = environment.DefaultProbePause
	}
	return &Provider{cfg: cfg, build: make(chan struct{}, 1), active: map[string]struct{}{}}, nil
}

// Environment returns the docker environment for the target. It fits environment.Kinds. It reads
// nothing and starts nothing: the Dockerfile is read when the environment is used.
func (p *Provider) Environment(target environment.Target) (environment.Environment, error) {
	return &env{p: p, target: target}, nil
}

// env is the docker environment of one target.
type env struct {
	p      *Provider
	target environment.Target
}

var _ environment.Environment = (*env)(nil)

// Kind implements environment.Environment.
func (*env) Kind() core.EnvironmentKind { return core.EnvironmentDocker }

// spec reads what the task asks of its image and refuses a task a container cannot serve.
func (e *env) spec(ctx context.Context) (Spec, error) {
	spec, err := e.p.cfg.Source.Spec(ctx, e.target)
	if err != nil {
		return Spec{}, err
	}
	if err := refusal(spec, e.p.cfg.Backend.Kind()); err != nil {
		return Spec{}, err
	}
	return spec, nil
}

// Fingerprint implements environment.Environment. It pins the exact image the task's Dockerfile
// builds, which it builds first if the engine does not have it, and the machine the image runs
// on.
func (e *env) Fingerprint(ctx context.Context) (core.Fingerprint, error) {
	spec, err := e.spec(ctx)
	if err != nil {
		return core.Fingerprint{}, err
	}
	img, err := e.p.image(ctx, spec, io.Discard)
	if err != nil {
		return core.Fingerprint{}, err
	}
	m, err := e.p.cfg.Backend.Machine(ctx)
	if err != nil {
		return core.Fingerprint{}, err
	}
	return core.Fingerprint{
		Kind:        core.EnvironmentDocker,
		Backend:     e.p.cfg.Backend.Kind(),
		ImageDigest: img.ID,
		Facts:       facts(m, img),
	}, nil
}

// image returns the image the Dockerfile builds. An image the engine already has under the
// Dockerfile's tag is not built again. Builds are one at a time, so that a Fingerprint and a Run
// that ask for the same image build it once; the build's progress goes to log.
func (p *Provider) image(ctx context.Context, spec Spec, log io.Writer) (Image, error) {
	select {
	case p.build <- struct{}{}:
		defer func() { <-p.build }()
	case <-ctx.Done():
		return Image{}, ctx.Err()
	}
	tag := imageTag(spec.Dockerfile)
	if img, ok, err := p.cfg.Engine.Image(ctx, tag); err != nil || ok {
		return checked(img, err)
	}
	if err := p.cfg.Engine.Build(ctx, BuildSpec{Tag: tag, Dockerfile: spec.Dockerfile, Labels: Labels{LabelOwner: Owner}}, log); err != nil {
		return Image{}, err
	}
	img, ok, err := p.cfg.Engine.Image(ctx, tag)
	if err == nil && !ok {
		err = &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: the engine built %s and does not have it", tag)}
	}
	return checked(img, err)
}

// checked passes err on, and refuses an image the fingerprint could not pin.
func checked(img Image, err error) (Image, error) {
	if err != nil {
		return Image{}, err
	}
	if !imageID.MatchString(img.ID) {
		return Image{}, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: the engine named an image %q, which is not an exact digest", img.ID)}
	}
	return img, nil
}

// claim records that a live run owns the container, and reports false if one already does.
func (p *Provider) claim(name string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if _, taken := p.active[name]; taken {
		return false
	}
	p.active[name] = struct{}{}
	return true
}

func (p *Provider) release(name string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.active, name)
}

// owned returns the containers live runs own.
func (p *Provider) owned() map[string]struct{} {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make(map[string]struct{}, len(p.active))
	for name := range p.active {
		out[name] = struct{}{}
	}
	return out
}

// sweep removes the containers of the scope that no live run of this process owns. Those are what
// a caller that died left behind: its container stops by itself when its input closes, and the
// next run removes it. A sweep that fails fails the run, since a container left running beside a
// job is a lie in the job's numbers.
func (p *Provider) sweep(ctx context.Context) error {
	failed := func(err error) error {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
			"docker: cannot clear the containers earlier runs left behind (%v); not starting, so that none runs beside this job", err)}
	}
	found, err := p.cfg.Engine.Containers(ctx, scopeLabels(p.cfg.Scope))
	if err != nil {
		return failed(err)
	}
	for _, name := range leftovers(found, p.owned()) {
		if err := p.cfg.Engine.Remove(ctx, name); err != nil {
			return failed(err)
		}
	}
	return nil
}
