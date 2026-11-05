package wire

import (
	"slices"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// This file carries what a supervisor of a check or measure job is given: the plan the daemon
// made. What the job finds goes back as lifecycle.Passed, converted in result.go.

func planCommand(c *task.Command) *cliv1.PlanCommand {
	if c == nil {
		return nil
	}
	return &cliv1.PlanCommand{Argv: slices.Clone(c.Argv), Timeout: durationpb.New(c.Timeout)}
}

func toPlanCommand(field string, c *cliv1.PlanCommand) (task.Command, error) {
	if c.GetTimeout() == nil {
		return task.Command{}, invalid("%s.timeout: a plan's command has a time limit", field)
	}
	if err := c.GetTimeout().CheckValid(); err != nil {
		return task.Command{}, invalid("%s.timeout: %v", field, err)
	}
	return task.Command{Argv: slices.Clone(c.GetArgv()), Timeout: c.GetTimeout().AsDuration()}, nil
}

// flowPlan returns the contract's message for a plan.
func flowPlan(p *flow.Plan) *cliv1.FlowPlan {
	if p == nil {
		return nil
	}
	out := &cliv1.FlowPlan{
		Kind: JobKind(p.Kind), Task: p.Task, TaskVersion: p.TaskVersion, Key: string(p.Key),
		Environment: Fingerprint(p.Environment), Build: planCommand(p.Build),
		Check:          planCommand(&p.Check),
		Samples:        p.Samples,
		CheckPassJobId: string(p.CheckPass),
		Candidate:      planCandidate(p.Candidate),
	}
	if p.Kind == core.JobMeasure {
		out.Benchmark = planCommand(&p.Benchmark)
		out.ResultFormat = &cliv1.ResultFormat{Protocol2: p.Result.Protocol2, JsonArgument: p.Result.JSONArgument, Field: p.Result.Field}
		out.Metric = &cliv1.Metric{Name: p.Metric.Name, Direction: direction(p.Metric.Direction)}
		out.Objectives = Objectives(p.Objectives)
	}
	return out
}

// toFlowPlan returns the domain plan for the contract's message, held to flow.Plan.Validate.
func toFlowPlan(field string, p *cliv1.FlowPlan) (*flow.Plan, error) {
	if p == nil {
		return nil, nil
	}
	kind, err := ToJobKind(field+".kind", p.GetKind())
	if err != nil {
		return nil, err
	}
	env, err := ToFingerprint(field+".environment", p.GetEnvironment())
	if err != nil {
		return nil, err
	}
	out := flow.Plan{
		Kind: kind, Task: p.GetTask(), TaskVersion: p.GetTaskVersion(), Key: core.JobKey(p.GetKey()), Environment: env,
		Samples: p.GetSamples(), CheckPass: core.JobID(p.GetCheckPassJobId()),
	}
	if p.GetBuild() != nil {
		b, err := toPlanCommand(field+".build", p.GetBuild())
		if err != nil {
			return nil, err
		}
		out.Build = &b
	}
	if p.GetCheck() != nil {
		if out.Check, err = toPlanCommand(field+".check", p.GetCheck()); err != nil {
			return nil, err
		}
	}
	if p.GetBenchmark() != nil {
		if out.Benchmark, err = toPlanCommand(field+".benchmark", p.GetBenchmark()); err != nil {
			return nil, err
		}
	}
	if f := p.GetResultFormat(); f != nil {
		out.Result = task.Result{Protocol2: f.GetProtocol2(), JSONArgument: f.GetJsonArgument(), Field: f.GetField()}
	}
	if m := p.GetMetric(); m != nil {
		if out.Metric.Direction, err = toDirection(field+".metric.direction", m.GetDirection()); err != nil {
			return nil, err
		}
		out.Metric.Name = m.GetName()
	}
	if out.Objectives, err = toObjectives(field+".objectives", p.GetObjectives(), toDirection); err != nil {
		return nil, err
	}
	if out.Candidate, err = toPlanCandidate(field+".candidate", p.GetCandidate()); err != nil {
		return nil, err
	}
	if err := out.Validate(); err != nil {
		return nil, err
	}
	return &out, nil
}
