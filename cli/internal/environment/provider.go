package environment

import (
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Target is what an environment is asked to run commands for: the task of a repository, at one
// commit. The host does not need it. The docker environment does, since the image a task runs in
// is built from a file of the task as it is at that commit.
type Target struct {
	// Repository is the absolute path of the git repository.
	Repository string
	// Commit is the exact commit the commands run at.
	Commit core.Commit
	// Task is the name of the task, a directory of .vibesys/tasks.
	Task string
}

// Environments hands out the Environment of a kind for a target. The daemon asks it for a job's
// fingerprint and a supervisor for the environment that runs the job, so both see the same kind.
//
// It fails with core.FailureUnavailable when this build or this machine has no environment of
// that kind, and never answers with another kind in its place.
type Environments interface {
	Environment(kind core.EnvironmentKind, target Target) (Environment, error)
}

// None is an Environments with no environment in it. A build that has not wired one in, and a
// test of what happens when none can be reached, use it: every request is unavailable, and says
// so, instead of falling back to some other kind.
type None struct{}

// Environment implements Environments.
func (None) Environment(kind core.EnvironmentKind, _ Target) (Environment, error) {
	return nil, unavailable(kind)
}

func unavailable(kind core.EnvironmentKind) error {
	return &core.Failure{
		Kind:    core.FailureUnavailable,
		Message: fmt.Sprintf("this build has no %q environment to run jobs in", kind),
	}
}

// Registry is an Environments over the environments a build has, by kind. A kind it lacks is
// unavailable, as with None. It hands out the same environment whatever the target.
type Registry map[core.EnvironmentKind]Environment

var _ Environments = Registry(nil)

// Environment implements Environments.
func (r Registry) Environment(kind core.EnvironmentKind, _ Target) (Environment, error) {
	if e, ok := r[kind]; ok && e != nil {
		return e, nil
	}
	return nil, unavailable(kind)
}

// Kinds is an Environments made of one function per kind, which is how the shipped binary wires
// its host and docker environments together. A kind with no function is unavailable.
type Kinds map[core.EnvironmentKind]func(Target) (Environment, error)

var _ Environments = Kinds(nil)

// Environment implements Environments.
func (k Kinds) Environment(kind core.EnvironmentKind, target Target) (Environment, error) {
	if build, ok := k[kind]; ok {
		return build(target)
	}
	return nil, unavailable(kind)
}
