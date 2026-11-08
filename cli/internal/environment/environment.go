// Package environment declares Environment, where a job's commands run.
//
// The two kinds are host and docker. Both pass one contract suite, so a job flow written against
// Environment does not know which it has. Which kind a task gets, and the refusal to mix kinds,
// are rules in internal/core and the wiring, not here.
package environment

import (
	"context"
	"fmt"
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

	// MachineReader reads the machine the environment's commands run on, which is not the
	// daemon's when the environment runs them on another machine.
	MachineReader

	// Fingerprint returns the environment's identity: what a result's comparability and a check
	// pass's reuse are judged by (core.Fingerprint.Equal). For docker it pins the exact image
	// digest; where the container ran is recorded among its facts and does not count. It returns the same
	// value for the same environment, and fails with core.FailureUnavailable when the
	// environment cannot be reached.
	Fingerprint(ctx context.Context) (core.Fingerprint, error)

	// Run executes one command in the checkout, with its service if the execution names one,
	// writing the combined output to out as it appears. An environment that waits before the
	// command starts, as for a cluster's queue, may first write to out whole lines that say why,
	// each starting with "[" and its name; it writes nothing of its own otherwise.
	//
	// It ends the whole process group of the command and of the service on every path: normal
	// exit, timeout, cancellation of ctx and a service that never became ready, so nothing
	// outlives the call. It says what the attempt did as a closed union: core.NotStarted when no
	// process of the execution started, core.Ended with how it ended when the environment saw it
	// end, and core.Unobserved when a process started and the environment cannot vouch for its
	// end or for what it left. A command that exits non-zero, a timeout and a cancellation are
	// endings. An attempt that failed while ctx was being cancelled is core.Ended with
	// core.ExecCancelled: the cancel is what ended it.
	Run(ctx context.Context, execution core.Execution, out io.Writer) core.Attempt

	// Candidate returns the candidate's image of a task of two images, as the target's commit has
	// its Dockerfile and build context, building it when the environment does not have an image of
	// the same Dockerfile and context already. The build's output goes to log. It fails with
	// core.FailureInvalidInput naming the path for a Dockerfile or a context the commit lacks, or a
	// base the task does not allow, with the end of the build's output for a build that failed, and
	// with core.FailureInvalidInput for an environment that runs no images, such as the host: a
	// task of two images runs only where images do.
	Candidate(ctx context.Context, build core.CandidateBuild, log io.Writer) (core.CandidateImage, error)

	// Serve starts a job-scoped server and waits until it is ready, and returns it running, with a
	// nil attempt; it outlives the call, and every Run of the Service runs against it. Otherwise it
	// returns a nil Service and how the start ended, as Run says it: core.NotStarted when nothing
	// started (an invalid service, or an environment that runs no images, such as the host, which
	// refuses with core.FailureInvalidInput), core.Ended with ExecServiceNotReady, ExecServiceExited
	// or ExecCancelled, with the server's last lines, and core.Unobserved when it cannot vouch for
	// what started. Nothing of a start that failed outlives the call.
	//
	// The server's output goes to out for as long as it runs, so out must take writes from it while
	// a Run of the Service writes to its own.
	Serve(ctx context.Context, s core.JobService, out io.Writer) (Service, core.Attempt)
}

// Service is a job-scoped server an Environment started: it runs until Close, across the commands
// that run against it. Its methods are safe for concurrent use.
type Service interface {
	// Run runs one command against the server, as Environment.Run does, with the server's address
	// in the job environment's VIBESYS_SERVICE_URL. The execution names no service and no candidate
	// of its own. The server ending while the command runs ends the run with ExecServiceExited, and
	// a server that ended before the run runs nothing: core.NotStarted, with ServiceEnded. Run ends
	// the command's processes on every path and never the server's.
	Run(ctx context.Context, x core.Execution, out io.Writer) core.Attempt

	// Alive reports whether the server still runs, as far as the environment saw: false once its
	// process ended or was lost. It does not block.
	Alive() bool

	// Close ends the server and everything of it on every path, whatever the caller's context, after
	// asking it to terminate and giving it the grace period, and reports what it could not clean
	// up. It is idempotent: a later call reports nothing.
	Close() error
}

// ServiceEnded is the failure of a Run of a Service whose server has ended.
func ServiceEnded() core.Failure {
	return core.Failure{Kind: core.FailureUnavailable, Message: "the job's server has ended, so no command runs against it"}
}

// NoService is what an environment that runs no job-scoped server answers Serve with.
func NoService(kind core.EnvironmentKind) core.Attempt {
	return core.NotStarted{Failure: core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
		"the %s environment runs no job-scoped server: a task's [service] runs in a container of its image, beside the commands that run against it; run it in docker", kind)}}
}

// NoImages is what an environment that runs no images answers Candidate with.
func NoImages(kind core.EnvironmentKind) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
		"the %s environment runs no images, and a task of two images runs its candidate in an image of its own; run it in docker", kind)}
}

// MachineReader reads a machine. A job's results record what it reads of the machine the job ran
// on.
type MachineReader interface {
	// Machine returns what does not change while the daemon runs: host identity, operating
	// system, CPU and memory.
	Machine(ctx context.Context) (core.MachineFacts, error)

	// Load samples the machine's current load. It is reported and never acted on.
	Load(ctx context.Context) (core.MachineLoad, error)
}
