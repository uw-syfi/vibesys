package wire

import (
	"fmt"
	"slices"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// This file carries what a supervisor of a check, measure or trace job is given: the plan the daemon
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
		Service:        planService(p.Service),
	}
	if p.Kind == core.JobMeasure {
		out.Benchmark = planCommand(&p.Benchmark)
		out.ResultFormat = resultFormat(p.Result)
		out.Metric = &cliv1.Metric{Name: p.Metric.Name, Direction: direction(p.Metric.Direction), Unit: p.Metric.Unit}
		for _, m := range p.Others {
			out.Others = append(out.Others, &cliv1.Metric{Name: m.Name, Direction: direction(m.Direction), Unit: m.Unit})
		}
		out.Objectives = Objectives(p.Objectives)
	}
	if p.Kind == core.JobTrace {
		out.Trace = planCommand(&p.Trace)
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
	if p.GetTrace() != nil {
		if out.Trace, err = toPlanCommand(field+".trace", p.GetTrace()); err != nil {
			return nil, err
		}
	}
	if f := p.GetResultFormat(); f != nil {
		if out.Result, err = toResultFormat(field+".result_format", f); err != nil {
			return nil, err
		}
	}
	if m := p.GetMetric(); m != nil {
		if out.Metric.Direction, err = toDirection(field+".metric.direction", m.GetDirection()); err != nil {
			return nil, err
		}
		out.Metric.Name = m.GetName()
		out.Metric.Unit = m.GetUnit()
	}
	for i, m := range p.GetOthers() {
		d, err := toDirection(fmt.Sprintf("%s.others[%d].direction", field, i), m.GetDirection())
		if err != nil {
			return nil, err
		}
		out.Others = append(out.Others, core.Metric{Name: m.GetName(), Direction: d, Unit: m.GetUnit()})
	}
	if out.Objectives, err = toObjectives(field+".objectives", p.GetObjectives(), toDirection); err != nil {
		return nil, err
	}
	if out.Candidate, err = toPlanCandidate(field+".candidate", p.GetCandidate()); err != nil {
		return nil, err
	}
	if out.Service, err = toPlanService(field+".service", p.GetService()); err != nil {
		return nil, err
	}
	if err := out.Validate(); err != nil {
		return nil, err
	}
	return &out, nil
}

// planService is the contract's message for a job-scoped server, nil for none.
func planService(s *task.Service) *cliv1.PlanService {
	if s == nil {
		return nil
	}
	return &cliv1.PlanService{
		Serve: slices.Clone(s.Serve), Ready: s.Ready, StartupTimeout: durationpb.New(s.StartupTimeout),
		ResetCommand: planCommand(&s.Reset), ShareWithCheck: s.ShareWithCheck,
	}
}

// toPlanService returns the job-scoped server of the contract's message, nil for none.
func toPlanService(field string, s *cliv1.PlanService) (*task.Service, error) {
	if s == nil {
		return nil, nil
	}
	if s.GetStartupTimeout() == nil {
		return nil, invalid("%s.startup_timeout: a job-scoped server has a startup time", field)
	}
	if err := s.GetStartupTimeout().CheckValid(); err != nil {
		return nil, invalid("%s.startup_timeout: %v", field, err)
	}
	if s.GetResetCommand() == nil {
		return nil, invalid("%s.reset_command: a job-scoped server has a reset", field)
	}
	reset, err := toPlanCommand(field+".reset_command", s.GetResetCommand())
	if err != nil {
		return nil, err
	}
	return &task.Service{
		Serve: slices.Clone(s.GetServe()), Ready: s.GetReady(), StartupTimeout: s.GetStartupTimeout().AsDuration(),
		Reset: reset, ShareWithCheck: s.GetShareWithCheck(),
	}, nil
}

// resultFormat is how a task's benchmark reports a result, as the message states it: nil when the
// task says nothing (a plan of a job that scores nothing).
func resultFormat(r task.Result) *cliv1.ResultFormat {
	switch {
	case r.Protocol2:
		return &cliv1.ResultFormat{Format: &cliv1.ResultFormat_EvaluatorStream{EvaluatorStream: &cliv1.EvaluatorStream{}}}
	case r.JSONArgument != "":
		return &cliv1.ResultFormat{Format: &cliv1.ResultFormat_ScoredField{ScoredField: &cliv1.ScoredField{Flag: r.JSONArgument, Field: r.Field}}}
	}
	return nil
}

func toResultFormat(field string, f *cliv1.ResultFormat) (task.Result, error) {
	switch format := f.GetFormat().(type) {
	case *cliv1.ResultFormat_EvaluatorStream:
		return task.Result{Protocol2: true}, nil
	case *cliv1.ResultFormat_ScoredField:
		return task.Result{JSONArgument: format.ScoredField.GetFlag(), Field: format.ScoredField.GetField()}, nil
	}
	// A plan written before the oneof names its format in the earlier fields.
	switch {
	case f.GetProtocol2():
		return task.Result{Protocol2: true}, nil
	case f.GetJsonArgument() != "":
		return task.Result{JSONArgument: f.GetJsonArgument(), Field: f.GetField()}, nil
	}
	return task.Result{}, invalid("%s names no format", field)
}
