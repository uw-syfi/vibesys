//go:build !unix

package host

import (
	"context"
	"io"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// DefaultProbePause is how long Run waits between two readiness probes of a service.
const DefaultProbePause = 500 * time.Millisecond

// Config configures New.
type Config struct {
	Backend    backend.Backend
	Timers     clock.Timers
	ProbePause time.Duration
}

// Environment is the host environment, which this operating system cannot provide.
type Environment struct{}

// New returns an environment whose every method fails as unavailable.
func New(Config) *Environment { return &Environment{} }

func unsupported() error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "the host environment needs a unix operating system"}
}

// Kind implements environment.Environment.
func (*Environment) Kind() core.EnvironmentKind { return core.EnvironmentHost }

// Fingerprint implements environment.Environment.
func (*Environment) Fingerprint(context.Context) (core.Fingerprint, error) {
	return core.Fingerprint{}, unsupported()
}

// Run implements environment.Environment.
func (*Environment) Run(context.Context, core.Execution, io.Writer) (core.ExecOutcome, error) {
	return core.ExecOutcome{}, unsupported()
}
