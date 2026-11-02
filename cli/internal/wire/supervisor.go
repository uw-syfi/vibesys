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
	if o.NotAttempted != nil {
		out.Result = &cliv1.SupervisorOutcome_NotAttempted{NotAttempted: Failure(*o.NotAttempted)}
	} else {
		out.Result = &cliv1.SupervisorOutcome_Executed{Executed: &cliv1.ExecutionOutcome{
			Kind: ExecutionKind(o.Exec.Kind), ExitCode: int32(o.Exec.ExitCode),
		}}
	}
	if o.CancelAsked {
		reason := o.CancelReason
		out.CancelReason = &reason
	}
	out.Found, out.Reason, out.Rejected = foundResult(o.Result), o.Reason, o.Rejected
	return out
}

// ToOutcome returns the domain outcome for the contract's message. A message that says neither
// that the command ran nor that it could not be attempted is an outcome the environment could
// not observe, never a success.
func ToOutcome(o *cliv1.SupervisorOutcome) lifecycle.Outcome {
	out := lifecycle.Outcome{CancelAsked: o.CancelReason != nil, CancelReason: o.GetCancelReason()}
	switch r := o.GetResult().(type) {
	case *cliv1.SupervisorOutcome_NotAttempted:
		f := ToFailure(r.NotAttempted)
		out.NotAttempted = &f
	case *cliv1.SupervisorOutcome_Executed:
		out.Exec = core.ExecOutcome{Kind: ToExecKind(r.Executed.GetKind()), ExitCode: int(r.Executed.GetExitCode())}
	default:
		out.Exec = core.ExecOutcome{Kind: core.ExecUnknown}
	}
	out.Rejected, out.Reason = o.GetRejected(), o.GetReason()
	if found, err := toFoundResult("outcome.found", o.GetFound()); err != nil {
		// A result that cannot be read is not saved, and a job that claims one is not a success.
		out.Exec, out.NotAttempted = core.ExecOutcome{Kind: core.ExecUnknown}, nil
	} else {
		out.Result = found
	}
	return out
}
