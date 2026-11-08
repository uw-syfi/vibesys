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
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// Config configures New. Every field but ProbePause is required.
type Config struct {
	Engine Engine
	// Source reads the files of a task at a commit.
	Source Source
	// Backend describes the machine the engine runs containers on: what the fingerprint is made of.
	Backend backend.Backend
	// Timers makes every timeout and pause Run waits on. The first timer a Run arms is the
	// execution's limit, with the duration of its Timeout; later ones are pauses between probes, and the grace period armed when the limit is
	// reached or the run is cancelled.
	Timers clock.Timers
	// IDs names the runs.
	IDs IDs
	// Scope is whose containers these are: a run removes the containers of its scope that no live
	// run of this process owns, so two live runs in different processes must not share one. A
	// daemon uses one scope (ScopeFor) for the jobs it runs one at a time, and one per job's runs
	// where it runs several at once.
	Scope string
	// User is the user and group the commands run as. It is the invoking user, so the files the
	// commands write are theirs.
	User User
	// ProbePause is the pause between two probes. Zero means environment.DefaultProbePause.
	ProbePause time.Duration
	// Grace is how long the commands get, after a cancel or the limit asks them to terminate, to
	// clean up before the container is removed. Zero means environment.DefaultGrace.
	Grace time.Duration

	// Machine is where the engine's containers run, as a run's files see it. Nil means Shared,
	// as User: the engine runs on this machine and mounts the caller's directories.
	Machine Machine
	// Placement names the placement of the operator config these containers run on. Every
	// fingerprint records it among its facts, for diagnosis; it takes no part in which results
	// are compared, since placements are interchangeable. It is required.
	Placement string
	// Resources are what the placement gives every container, at most one of each kind: GPUs,
	// a cluster, read-only directories. A task that needs a kind the placement does not give is
	// refused, and so is a task for an accelerator without GPUs. For a task of two images, the GPUs
	// go to the candidate's container alone.
	Resources []resource.Resource

	// Candidates reads what builds a candidate's image, for a task of two images. Nil builds none.
	Candidates CandidateSource
	// Builds is the lock of the builds of each tag on the engine, shared by the providers of every
	// placement whose engine is the same Docker. Nil gives the provider one of its own.
	Builds *Builds
	// Probe is the absolute path, on the engine's machine, of a program for the engine's platform
	// that asks a URL once and exits 0 for a 2xx answer, when called with ProbeCommand and the URL:
	// this binary, which is static. It is mounted read-only at ProbeMount in the container of a
	// task's image, to probe the candidate's server beside it. Empty runs no task of two images.
	Probe string
}

// Provider makes the docker environments of one process. They share what must be one per
// process: the record of which containers live runs own, and the token that lets it build one
// image at a time. Builds of one tag by providers that share an engine are one at a time through
// Config.Builds. Create it with New.
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
	case cfg.Probe != "" && cfg.Probe[0] != '/':
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("docker: the probe program %q is not an absolute path", cfg.Probe)}
	case cfg.User.UID < 0 || cfg.User.GID < 0:
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("docker: the user %d:%d is negative", cfg.User.UID, cfg.User.GID)}
	}
	if err := checkScope(cfg.Scope); err != nil {
		return nil, err
	}
	if err := core.ValidPlacementName(cfg.Placement); err != nil {
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: "docker: " + core.AsFailure(err).Message}
	}
	seen := map[core.ResourceKind]bool{}
	for _, r := range cfg.Resources {
		if r == nil || seen[r.Kind()] {
			return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("docker: the placement %q has a resource that is missing, or two of one kind", cfg.Placement)}
		}
		seen[r.Kind()] = true
	}
	if cfg.Machine == nil {
		cfg.Machine = Shared{As: cfg.User}
	}
	if cfg.ProbePause <= 0 {
		cfg.ProbePause = environment.DefaultProbePause
	}
	if cfg.Grace <= 0 {
		cfg.Grace = environment.DefaultGrace
	}
	if cfg.Builds == nil {
		cfg.Builds = NewBuilds()
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
	if err := refusal(spec, e.p.Offer()); err != nil {
		return Spec{}, err
	}
	return spec, nil
}

// Offer returns the placement as choosing one for a job sees it: its name and the kinds of
// resource it gives a container.
func (p *Provider) Offer() core.PlacementOffer {
	o := core.PlacementOffer{Name: p.cfg.Placement}
	for _, r := range p.cfg.Resources {
		o.Resources = append(o.Resources, r.Kind())
	}
	return o
}

// Claims returns what a job on the placement holds of its resources while it runs.
func (p *Provider) Claims() []core.Claim {
	var out []core.Claim
	for _, r := range p.cfg.Resources {
		out = append(out, r.Claims()...)
	}
	return out
}

// Fingerprint implements environment.Environment. It pins the exact image the task's files
// build, which it builds first if the engine does not have it, the machine the image runs on, the
// placement, and what identifies each resource its containers get.
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
	all := facts(m, img, e.p.cfg.Placement)
	for _, r := range e.p.cfg.Resources {
		got, err := r.Fingerprint(ctx)
		if err != nil {
			return core.Fingerprint{}, err
		}
		for name, value := range got {
			if _, taken := all[name]; taken {
				return core.Fingerprint{}, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: the %s resource reports the fact %q, which another part of the fingerprint has", r.Kind(), name)}
			}
			all[name] = value
		}
	}
	return core.Fingerprint{
		Kind:        core.EnvironmentDocker,
		Backend:     backendOf(spec),
		ImageDigest: img.ID,
		Facts:       all,
	}, nil
}

// Machine implements environment.Environment: the machine the engine runs containers on, which
// for an engine reached over ssh is that machine and not the daemon's.
func (e *env) Machine(ctx context.Context) (core.MachineFacts, error) {
	return e.p.cfg.Backend.Machine(ctx)
}

// Load implements environment.Environment.
func (e *env) Load(ctx context.Context) (core.MachineLoad, error) { return e.p.cfg.Backend.Load(ctx) }

// image returns the image the task's files build on the engine's local images of its bases. An
// image the engine already has under their tag (buildKey) is not built again. Builds are one at a
// time, and a build of the tag waits for one by another provider of the engine (Config.Builds), so
// that a Fingerprint and a Run, or jobs of two placements, that ask for the same image build it
// once; the build's progress goes to log.
func (p *Provider) image(ctx context.Context, spec Spec, log io.Writer) (Image, error) {
	select {
	case p.build <- struct{}{}:
		defer func() { <-p.build }()
	case <-ctx.Done():
		return Image{}, ctx.Err()
	}
	buildContext, err := Context(spec)
	if err != nil {
		return Image{}, err
	}
	bases, err := p.bases(ctx, spec.Dockerfile)
	if err != nil {
		return Image{}, err
	}
	tag := imageTag(buildKey(buildContext, bases))
	release, err := p.cfg.Builds.lock(ctx, tag, "[docker]", log)
	if err != nil {
		return Image{}, err
	}
	defer release()
	if img, ok, err := p.cfg.Engine.Image(ctx, tag); err != nil || ok {
		return checked(img, err)
	}
	if err := p.cfg.Engine.Build(ctx, BuildSpec{Tag: tag, Context: buildContext, Labels: Labels{LabelOwner: Owner}, Role: TaskImage}, log); err != nil {
		return Image{}, err
	}
	img, ok, err := p.cfg.Engine.Image(ctx, tag)
	if err == nil && !ok {
		err = &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: the engine built %s and does not have it", tag)}
	}
	return checked(img, err)
}

// bases looks up the engine's local image of each image the Dockerfile builds on (baseRefs).
func (p *Provider) bases(ctx context.Context, dockerfile []byte) ([]base, error) {
	var out []base
	for _, ref := range baseRefs(dockerfile) {
		img, ok, err := p.cfg.Engine.Image(ctx, ref)
		if err != nil {
			return nil, err
		}
		b := base{Ref: ref}
		if ok {
			b.ID = img.ID
		}
		out = append(out, b)
	}
	return out, nil
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
