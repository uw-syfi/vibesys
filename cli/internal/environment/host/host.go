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
const DefaultProbePause = 500 * time.Millisecond

// Config configures New.
type Config struct {
	// Backend describes the machine: what the fingerprint is made of.
	Backend backend.Backend
	// Timers makes every timeout and pause Run waits on. The first timer a Run arms is the
	// execution's limit, with the duration of its Timeout; later ones are pauses between probes.
	Timers clock.Timers
	// ProbePause is the pause between two probes. Zero means DefaultProbePause.
	ProbePause time.Duration
}

// Environment is the host environment. Create it with New.
type Environment struct {
	backend backend.Backend
	timers  clock.Timers
	pause   time.Duration
}

var _ environment.Environment = (*Environment)(nil)

// New returns a host environment.
func New(cfg Config) *Environment {
	pause := cfg.ProbePause
	if pause <= 0 {
		pause = DefaultProbePause
	}
	return &Environment{backend: cfg.Backend, timers: cfg.Timers, pause: pause}
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

// Run implements environment.Environment.
func (e *Environment) Run(ctx context.Context, x core.Execution, out io.Writer) (core.ExecOutcome, error) {
	if err := environment.Validate(x); err != nil {
		return core.ExecOutcome{}, err
	}
	if ctx.Err() != nil {
		return core.ExecOutcome{Kind: core.ExecCancelled}, nil
	}
	if out == nil {
		out = io.Discard
	}
	r, err := newRun(ctx, e, x, out)
	if err != nil {
		return core.ExecOutcome{}, err
	}
	outcome, err := r.execute()
	lost := r.end()
	switch {
	case err != nil:
		return core.ExecOutcome{}, err
	case lost != nil:
		// The command ran, but what it wrote was not kept: the environment cannot say what the
		// run did, as far as its output goes.
		return core.ExecOutcome{Kind: core.ExecUnknown}, lost
	}
	return outcome, nil
}
