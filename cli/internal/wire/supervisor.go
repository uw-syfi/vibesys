package wire

import (
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// Launch returns the contract's message for a launch.
func Launch(l lifecycle.Launch) *cliv1.Launch {
	return &cliv1.Launch{Repository: l.Repository, Task: l.Task, JobId: string(l.Job), Spec: JobSpec(l.Spec)}
}

// ToLaunch returns the domain launch for the contract's message, validated like a submission and
// held to what a supervisor is promised: a concrete environment kind and, for a command, a
// positive timeout.
func ToLaunch(l *cliv1.Launch) (lifecycle.Launch, error) {
	if l.GetRepository() == "" || l.GetTask() == "" || l.GetJobId() == "" {
		return lifecycle.Launch{}, invalid("launch: repository, task and job id are all required")
	}
	spec, err := ToJobSpec("launch.spec", l.GetSpec())
	if err != nil {
		return lifecycle.Launch{}, err
	}
	if spec.Environment == core.EnvironmentAuto {
		return lifecycle.Launch{}, invalid("launch.spec.environment: a launch names the environment kind, not unspecified")
	}
	if c, ok := spec.Settings.(core.CommandSettings); ok && c.Timeout <= 0 {
		return lifecycle.Launch{}, invalid("launch.spec.command.timeout: a launch carries a positive timeout")
	}
	return lifecycle.Launch{Repository: l.GetRepository(), Task: l.GetTask(), Job: core.JobID(l.GetJobId()), Spec: spec}, nil
}

// Outcome returns the contract's message for a supervisor's outcome.
func Outcome(o lifecycle.Outcome) *cliv1.SupervisorOutcome {
	out := &cliv1.SupervisorOutcome{}
	switch end := o.End.(type) {
	case lifecycle.Ran:
		out.Result = &cliv1.SupervisorOutcome_Executed{Executed: &cliv1.ExecutionOutcome{
			Kind: ExecutionKind(end.Exec.Kind), ExitCode: int32(end.Exec.ExitCode),
		}}
	case lifecycle.NotAttempted:
		out.Result = &cliv1.SupervisorOutcome_NotAttempted{NotAttempted: Failure(end.Failure)}
	case lifecycle.Passed:
		out.Result = &cliv1.SupervisorOutcome_Passed{Passed: result(end.Result)}
	case lifecycle.Rejected:
		out.Result = &cliv1.SupervisorOutcome_Rejected{Rejected: end.Reason}
	default:
		// An outcome with no end stays one: the receiver reads it as unobserved.
	}
	if o.CancelAsked {
		reason := o.CancelReason
		out.CancelReason = &reason
	}
	return out
}

// ToOutcome returns the domain outcome for the contract's message. A message that does not say how
// the job ended, or that claims a result that cannot be read, is an outcome the environment could
// not observe, never a success.
func ToOutcome(o *cliv1.SupervisorOutcome) lifecycle.Outcome {
	out := lifecycle.Outcome{CancelAsked: o.CancelReason != nil, CancelReason: o.GetCancelReason()}
	unobserved := lifecycle.Ran{Exec: core.ExecOutcome{Kind: core.ExecUnknown}}
	switch r := o.GetResult().(type) {
	case *cliv1.SupervisorOutcome_NotAttempted:
		out.End = lifecycle.NotAttempted{Failure: ToFailure(r.NotAttempted)}
	case *cliv1.SupervisorOutcome_Executed:
		out.End = lifecycle.Ran{Exec: core.ExecOutcome{Kind: ToExecKind(r.Executed.GetKind()), ExitCode: int(r.Executed.GetExitCode())}}
	case *cliv1.SupervisorOutcome_Passed:
		found, err := toResult("outcome.passed", r.Passed)
		if err != nil {
			out.End = unobserved
			break
		}
		out.End = lifecycle.Passed{Result: found}
	case *cliv1.SupervisorOutcome_Rejected:
		out.End = lifecycle.Rejected{Reason: r.Rejected}
	default:
		out.End = unobserved
	}
	return out
}
