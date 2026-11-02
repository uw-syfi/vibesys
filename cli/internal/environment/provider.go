package environment

import (
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Environments hands out the Environment of a kind. The daemon asks it for a job's fingerprint
// and a supervisor for the environment that runs the job, so both see the same kind.
//
// It fails with core.FailureUnavailable when this build or this machine has no environment of
// that kind, and never answers with another kind in its place.
type Environments interface {
	Environment(kind core.EnvironmentKind) (Environment, error)
}

// None is an Environments with no environment in it. A build that has not wired one in, and a
// test of what happens when none can be reached, use it: every request is unavailable, and says
// so, instead of falling back to some other kind.
type None struct{}

// Environment implements Environments.
func (None) Environment(kind core.EnvironmentKind) (Environment, error) {
	return nil, &core.Failure{
		Kind:    core.FailureUnavailable,
		Message: fmt.Sprintf("this build has no %q environment to run jobs in", kind),
	}
}

// Registry is an Environments over the environments a build has, by kind. A kind it lacks is
// unavailable, as with None.
type Registry map[core.EnvironmentKind]Environment

var _ Environments = Registry(nil)

// Environment implements Environments.
func (r Registry) Environment(kind core.EnvironmentKind) (Environment, error) {
	if e, ok := r[kind]; ok && e != nil {
		return e, nil
	}
	return None{}.Environment(kind)
}
