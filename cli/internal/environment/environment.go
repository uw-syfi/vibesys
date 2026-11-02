// Package environment declares Environment, where a job's commands run.
//
// The two kinds are host and docker. Both pass one contract suite, so a job flow written against
// Environment does not know which it has. Which kind a task gets, and the refusal to mix kinds,
// are rules in internal/core and the wiring, not here.
package environment

import (
	"context"
	"io"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// DefaultProbePause is how long an environment waits between two readiness probes of a service,
// unless it is configured with another pause.
const DefaultProbePause = 500 * time.Millisecond

// Environment runs commands in a clean checkout and says what it is.
//
// The environment is chosen once at run start and pinned. If it is unavailable, such as Docker
// that is not running, its methods fail with core.FailureUnavailable: there is no silent
// fallback to another kind.
type Environment interface {
	// Kind is the kind this environment is.
	Kind() core.EnvironmentKind

	// Fingerprint returns the environment's identity: what a result's comparability and a check
	// pass's reuse are judged by. For docker it pins the exact image digest. It returns the same
	// value for the same environment, and fails with core.FailureUnavailable when the
	// environment cannot be reached.
	Fingerprint(ctx context.Context) (core.Fingerprint, error)

	// Run executes one command in the checkout, with its service if the execution names one,
	// writing the combined output to out as it appears.
	//
	// It ends the whole process group of the command and of the service on every path: normal
	// exit, timeout, cancellation of ctx and a service that never became ready, so nothing
	// outlives the call. It returns the outcome as a value: a command that exits non-zero, a
	// timeout and a cancellation are outcomes, not errors. It returns an error only when the
	// execution could not be attempted or its result could not be observed, and an outcome of
	// core.ExecUnknown when the environment cannot say what happened.
	Run(ctx context.Context, execution core.Execution, out io.Writer) (core.ExecOutcome, error)
}
