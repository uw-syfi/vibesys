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

	// Lab holds a lab (core.LabSpec): it starts the containers a run of the execution would have,
	// with nothing in them but what keeps them up, and calls hold with them (Bench) once they are
	// up and what their resources ask them to check passed. For an execution with a candidate's
	// image the rooms are the candidate's, with the placement's GPUs, and the task's beside it in
	// its network, as for a profile; otherwise the one room is the task's image. The lab's time is
	// the execution's Timeout, armed when hold is called. Once hold returns, Lab removes the
	// containers and brings the artifacts back, on every path, and says what the attempt did as
	// Run does: core.Ended with hold's ending. The execution passes ValidateLab. An environment
	// that holds no labs, such as the host, answers NoLab.
	Lab(ctx context.Context, execution core.Execution, out io.Writer, hold HoldLab) core.Attempt
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
