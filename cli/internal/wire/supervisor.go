package wire

import (
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// Launch returns the contract's message for a launch.
func Launch(l lifecycle.Launch) *cliv1.Launch {
	return &cliv1.Launch{Repository: l.Repository, Task: l.Task, JobId: string(l.Job), Spec: JobSpec(l.Spec), Plan: flowPlan(l.Plan)}
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
	plan, err := toFlowPlan("launch.plan", l.GetPlan())
	if err != nil {
		return lifecycle.Launch{}, err
	}
	switch kind := spec.Settings.Kind(); {
	case kind == core.JobCommand && plan != nil:
		return lifecycle.Launch{}, invalid("launch.plan: a command job has no plan")
	case kind != core.JobCommand && plan == nil:
		return lifecycle.Launch{}, invalid("launch.plan: a %s job carries the plan it runs", kind)
	case plan != nil && (plan.Kind != kind || plan.Environment.Kind != spec.Environment):
		return lifecycle.Launch{}, invalid("launch.plan: the plan is for a %s job in %s, and the job is a %s job in %s", plan.Kind, plan.Environment.Kind, kind, spec.Environment)
	}
	return lifecycle.Launch{Repository: l.GetRepository(), Task: l.GetTask(), Job: core.JobID(l.GetJobId()), Spec: spec, Plan: plan}, nil
}

// Outcome returns the contract's message for a supervisor's outcome.
func Outcome(o lifecycle.Outcome) *cliv1.SupervisorOutcome {
	out := &cliv1.SupervisorOutcome{}
	switch end := o.End.(type) {
	case lifecycle.CommandEnded:
		out.Result = &cliv1.SupervisorOutcome_Command{Command: commandEnd(end.Attempt)}
	case lifecycle.FlowEnded:
		out.Result = &cliv1.SupervisorOutcome_Flow{Flow: flowEnd(end.Verdict)}
	default:
		// An outcome with no end stays one: the receiver reads it as an ending it does not know.
	}
	if o.CancelAsked {
		reason := o.CancelReason
		out.CancelReason = &reason
	}
	return out
}

func commandEnd(a core.Attempt) *cliv1.CommandEnd {
	out := &cliv1.CommandEnd{}
	switch a := a.(type) {
	case core.NotStarted:
		out.Attempt = &cliv1.CommandEnd_NotStarted{NotStarted: Failure(a.Failure)}
	case core.Ended:
		out.Attempt = &cliv1.CommandEnd_Ended{Ended: execution(a.Exec)}
	case core.Unobserved:
		out.Attempt = &cliv1.CommandEnd_Unobserved{Unobserved: Failure(a.Failure)}
	default:
		// An attempt with no variant stays one: the receiver reads it as one nobody saw end.
	}
	return out
}

func flowEnd(v lifecycle.Verdict) *cliv1.FlowEnd {
	out := &cliv1.FlowEnd{}
	switch v := v.(type) {
	case lifecycle.Passed:
		out.Verdict = &cliv1.FlowEnd_Passed{Passed: Result(v.Result)}
	case lifecycle.Stopped:
		out.Verdict = &cliv1.FlowEnd_Stopped{Stopped: execution(v.Exec)}
	case lifecycle.NotAttempted:
		out.Verdict = &cliv1.FlowEnd_NotAttempted{NotAttempted: Failure(v.Failure)}
	case lifecycle.Rejected:
		out.Verdict = &cliv1.FlowEnd_Rejected{Rejected: v.Reason}
	case lifecycle.Lost:
		out.Verdict = &cliv1.FlowEnd_Lost{Lost: Failure(v.Failure)}
	default:
		// A verdict with no variant stays one: the receiver reads it as lost.
	}
	return out
}

func execution(e core.ExecOutcome) *cliv1.ExecutionOutcome {
	return &cliv1.ExecutionOutcome{Kind: ExecutionKind(e.Kind), ExitCode: int32(e.ExitCode)}
}

// ToOutcome returns the domain outcome for the contract's message. A message that does not say how
// the job ended, says it in a way this build cannot read, or claims a result that cannot be read,
// is an ending nobody saw, never a success.
func ToOutcome(o *cliv1.SupervisorOutcome) lifecycle.Outcome {
	out := lifecycle.Outcome{CancelAsked: o.CancelReason != nil, CancelReason: o.GetCancelReason()}
	switch r := o.GetResult().(type) {
	case *cliv1.SupervisorOutcome_Command:
		out.End = lifecycle.CommandEnded{Attempt: toAttempt(r.Command)}
	case *cliv1.SupervisorOutcome_Flow:
		out.End = lifecycle.FlowEnded{Verdict: toVerdict(r.Flow)}
	default:
		// No end, or one of the members that are not sent: which kind of job it is cannot be
		// told, and Settle reads a missing end as lost.
	}
	return out
}

// unread is the failure of an ending that the message does not say in a way this build can read.
func unread(err error) core.Failure {
	return core.Failure{Kind: core.FailureInternal, Message: "the supervisor's outcome cannot be read: " + err.Error()}
}

func toAttempt(c *cliv1.CommandEnd) core.Attempt {
	switch a := c.GetAttempt().(type) {
	case *cliv1.CommandEnd_NotStarted:
		return core.NotStarted{Failure: ToFailure(a.NotStarted)}
	case *cliv1.CommandEnd_Ended:
		exec, err := toExecution("outcome.command.ended", a.Ended)
		if err != nil {
			return core.Unobserved{Failure: unread(err)}
		}
		return core.Ended{Exec: exec}
	case *cliv1.CommandEnd_Unobserved:
		return core.Unobserved{Failure: ToFailure(a.Unobserved)}
	default:
		return core.Unobserved{Failure: unread(invalid("outcome.command: no attempt"))}
	}
}

func toVerdict(f *cliv1.FlowEnd) lifecycle.Verdict {
	switch v := f.GetVerdict().(type) {
	case *cliv1.FlowEnd_Passed:
		found, err := toResult("outcome.flow.passed", v.Passed)
		if err != nil {
			return lifecycle.Lost{Failure: unread(err)}
		}
		return lifecycle.Passed{Result: found}
	case *cliv1.FlowEnd_Stopped:
		exec, err := toExecution("outcome.flow.stopped", v.Stopped)
		if err != nil {
			return lifecycle.Lost{Failure: unread(err)}
		}
		return lifecycle.Stopped{Exec: exec}
	case *cliv1.FlowEnd_NotAttempted:
		return lifecycle.NotAttempted{Failure: ToFailure(v.NotAttempted)}
	case *cliv1.FlowEnd_Rejected:
		return lifecycle.Rejected{Reason: v.Rejected}
	case *cliv1.FlowEnd_Lost:
		return lifecycle.Lost{Failure: ToFailure(v.Lost)}
	default:
		return lifecycle.Lost{Failure: unread(invalid("outcome.flow: no verdict"))}
	}
}

func toExecution(field string, e *cliv1.ExecutionOutcome) (core.ExecOutcome, error) {
	kind, err := ToExecKind(field+".kind", e.GetKind())
	if err != nil {
		return core.ExecOutcome{}, err
	}
	return core.ExecOutcome{Kind: kind, ExitCode: int(e.GetExitCode())}, nil
}

// RunPhase is a supervisor's phase on the wire.
func RunPhase(p lifecycle.RunPhase) cliv1.SupervisorPhase {
	switch p {
	case lifecycle.RunParked:
		return cliv1.SupervisorPhase_SUPERVISOR_PHASE_PARKED
	case lifecycle.RunRunning:
		return cliv1.SupervisorPhase_SUPERVISOR_PHASE_RUNNING
	case lifecycle.RunFinished:
		return cliv1.SupervisorPhase_SUPERVISOR_PHASE_FINISHED
	case lifecycle.RunReleased:
		return cliv1.SupervisorPhase_SUPERVISOR_PHASE_RELEASED
	}
	return cliv1.SupervisorPhase_SUPERVISOR_PHASE_UNSPECIFIED
}

// ToRunPhase is a supervisor's phase from the wire. A phase this build cannot read is running, the
// one that makes the daemon wait.
func ToRunPhase(p cliv1.SupervisorPhase) lifecycle.RunPhase {
	switch p {
	case cliv1.SupervisorPhase_SUPERVISOR_PHASE_PARKED:
		return lifecycle.RunParked
	case cliv1.SupervisorPhase_SUPERVISOR_PHASE_FINISHED:
		return lifecycle.RunFinished
	case cliv1.SupervisorPhase_SUPERVISOR_PHASE_RELEASED:
		return lifecycle.RunReleased
	case cliv1.SupervisorPhase_SUPERVISOR_PHASE_RUNNING, cliv1.SupervisorPhase_SUPERVISOR_PHASE_UNSPECIFIED:
	}
	return lifecycle.RunRunning
}
