//go:build unix

package host

import (
	"context"
	"io"
	"strconv"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// DefaultProbePause is how long Run waits between two readiness probes of a service.
const DefaultProbePause = environment.DefaultProbePause

// Config configures New.
type Config struct {
	// Backend describes the machine: what the fingerprint is made of.
	Backend backend.Backend
	// Timers makes every timeout and pause Run waits on. The first timer a Run arms is the
	// execution's limit, with the duration of its Timeout; later ones are pauses between probes, and the grace period armed when the limit is reached or the
	// caller cancels.
	Timers clock.Timers
	// ProbePause is the pause between two probes. Zero means DefaultProbePause.
	ProbePause time.Duration
	// Grace is how long the commands get, after a cancel or the limit asks them to terminate, to
	// clean up before they are killed. Zero means environment.DefaultGrace.
	Grace time.Duration
}

// Environment is the host environment. Create it with New.
type Environment struct {
	backend backend.Backend
	timers  clock.Timers
	pause   time.Duration
	grace   time.Duration
}

var _ environment.Environment = (*Environment)(nil)

// New returns a host environment.
func New(cfg Config) *Environment {
	pause := cfg.ProbePause
	if pause <= 0 {
		pause = DefaultProbePause
	}
	grace := cfg.Grace
	if grace <= 0 {
		grace = environment.DefaultGrace
	}
	return &Environment{backend: cfg.Backend, timers: cfg.Timers, pause: pause, grace: grace}
}

// Kind implements environment.Environment.
func (*Environment) Kind() core.EnvironmentKind { return core.EnvironmentHost }

// Fingerprint implements environment.Environment. Results taken on machines with the same
// operating system, architecture, CPU model and CPU count are comparable, so those make the
// fingerprint; the machine's name and memory do not.
func (e *Environment) Fingerprint(ctx context.Context) (core.Fingerprint, error) {
	m, err := e.backend.Machine(ctx)
	if err != nil {
		return core.Fingerprint{}, err
	}
	return core.Fingerprint{
		Kind:    core.EnvironmentHost,
		Backend: e.backend.Kind(),
		Facts: map[string]string{
			"os":           m.OS,
			"arch":         m.Arch,
			"cpu_model":    m.CPUModel,
			"logical_cpus": strconv.FormatUint(uint64(m.LogicalCPUs), 10),
		},
	}, nil
}

// Machine implements environment.Environment: the commands run on this machine.
func (e *Environment) Machine(ctx context.Context) (core.MachineFacts, error) {
	return e.backend.Machine(ctx)
}

// Load implements environment.Environment.
func (e *Environment) Load(ctx context.Context) (core.MachineLoad, error) { return e.backend.Load(ctx) }

// Candidate implements environment.Environment: the host runs no images.
func (e *Environment) Candidate(context.Context, core.CandidateBuild, io.Writer) (core.CandidateImage, error) {
	return core.CandidateImage{}, environment.NoImages(core.EnvironmentHost)
}

// ImageContent implements environment.Environment: the host has no images.
func (e *Environment) ImageContent(context.Context, string) (string, bool, error) {
	return "", false, nil
}

// Lab implements environment.Environment: the host holds no containers, so no lab.
func (e *Environment) Lab(context.Context, core.Execution, io.Writer, environment.HoldLab) core.Attempt {
	return environment.NoLab(core.EnvironmentHost)
}

// Run implements environment.Environment.
func (e *Environment) Run(ctx context.Context, x core.Execution, out io.Writer) core.Attempt {
	if err := environment.Validate(x); err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	if x.Candidate != nil {
		return core.NotStarted{Failure: core.AsFailure(environment.NoImages(core.EnvironmentHost))}
	}
	if ctx.Err() != nil {
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	if out == nil {
		out = io.Discard
	}
	r, err := newRun(ctx, e, x, out)
	if err != nil {
		return environment.Finish(ctx, core.NotStarted{Failure: core.AsFailure(err)}, nil)
	}
	attempt := r.execute()
	// What the command wrote and was not kept makes its end one the environment cannot vouch for.
	return environment.Finish(ctx, attempt, r.end())
}
