package wire

import (
	"slices"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// This file carries what a supervisor of a check or measure job is given and what it finds: the
// plan the daemon made, and the result the job saves. A result goes through here only from the
// supervisor to the daemon.

var metricDirections = []struct {
	domain core.Direction
	wire   cliv1.Direction
}{
	{core.Maximize, cliv1.Direction_DIRECTION_MAXIMIZE},
	{core.Minimize, cliv1.Direction_DIRECTION_MINIMIZE},
}

func direction(d core.Direction) cliv1.Direction {
	for _, p := range metricDirections {
		if p.domain == d {
			return p.wire
		}
	}
	return cliv1.Direction_DIRECTION_UNSPECIFIED
}

func toDirection(field string, d cliv1.Direction) (core.Direction, error) {
	for _, p := range metricDirections {
		if p.wire == d {
			return p.domain, nil
		}
	}
	return "", invalid("%s: %s is not a direction", field, d)
}

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
	}
	if p.Kind == core.JobMeasure {
		out.Benchmark = planCommand(&p.Benchmark)
		out.ResultFormat = &cliv1.ResultFormat{Protocol2: p.Result.Protocol2, JsonArgument: p.Result.JSONArgument, Field: p.Result.Field}
		out.Metric = &cliv1.Metric{Name: p.Metric.Name, Direction: direction(p.Metric.Direction)}
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
	if err := out.Validate(); err != nil {
		return nil, err
	}
	return &out, nil
}

// foundResult returns the contract's message for the result a job found.
func foundResult(r *core.Result) *cliv1.Result {
	if r == nil {
		return nil
	}
	out := &cliv1.Result{
		JobId: string(r.Job), Key: string(r.Key), Task: r.Task, TaskVersion: r.TaskVersion,
		Commit: string(r.Commit), SchemaVersion: r.SchemaVersion, Environment: Fingerprint(r.Environment),
		Machine: &cliv1.MachineFacts{
			HostId: r.Machine.HostID, Os: r.Machine.OS, Arch: r.Machine.Arch, CpuModel: r.Machine.CPUModel,
			LogicalCpus: r.Machine.LogicalCPUs, MemoryBytes: r.Machine.MemoryBytes,
		},
		Load: &cliv1.MachineLoad{
			SampledAt: timestamp(r.Load.SampledAt), LoadAverage_1M: r.Load.LoadAverage1m,
			CpuBusyFraction: r.Load.CPUBusyFraction, MemoryAvailableBytes: r.Load.MemoryAvailableBytes,
		},
		RecordedAt: timestamp(r.RecordedAt),
	}
	switch o := r.Outcome.(type) {
	case core.CheckOutcome:
		out.Outcome = &cliv1.Result_Check{Check: &cliv1.CheckResult{Duration: durationpb.New(o.Duration)}}
	case core.MeasureOutcome:
		m := &cliv1.MeasureResult{
			Metric:  &cliv1.Metric{Name: o.Metric.Name, Direction: direction(o.Metric.Direction)},
			Samples: slices.Clone(o.Samples), CheckJobId: string(o.CheckJob),
		}
		for _, ob := range o.Observations {
			m.Observations = append(m.Observations, &cliv1.Observation{Name: ob.Name, Values: slices.Clone(ob.Values)})
		}
		out.Outcome = &cliv1.Result_Measure{Measure: m}
	}
	return out
}

// toFoundResult returns the domain result for the contract's message. A message with neither
// outcome is refused: a result always says what it measured.
func toFoundResult(field string, r *cliv1.Result) (*core.Result, error) {
	if r == nil {
		return nil, nil
	}
	env, err := ToFingerprint(field+".environment", r.GetEnvironment())
	if err != nil {
		return nil, err
	}
	out := core.Result{
		Job: core.JobID(r.GetJobId()), Key: core.JobKey(r.GetKey()), Task: r.GetTask(), TaskVersion: r.GetTaskVersion(),
		Commit: core.Commit(r.GetCommit()), SchemaVersion: r.GetSchemaVersion(), Environment: env,
		Machine: core.MachineFacts{
			HostID: r.GetMachine().GetHostId(), OS: r.GetMachine().GetOs(), Arch: r.GetMachine().GetArch(),
			CPUModel: r.GetMachine().GetCpuModel(), LogicalCPUs: r.GetMachine().GetLogicalCpus(), MemoryBytes: r.GetMachine().GetMemoryBytes(),
		},
		Load: core.MachineLoad{
			SampledAt: toTime(r.GetLoad().GetSampledAt()), LoadAverage1m: r.GetLoad().GetLoadAverage_1M(),
			CPUBusyFraction: r.GetLoad().GetCpuBusyFraction(), MemoryAvailableBytes: r.GetLoad().GetMemoryAvailableBytes(),
		},
		RecordedAt: toTime(r.GetRecordedAt()),
	}
	switch o := r.GetOutcome().(type) {
	case *cliv1.Result_Check:
		var d time.Duration
		if o.Check.GetDuration() != nil {
			if err := o.Check.GetDuration().CheckValid(); err != nil {
				return nil, invalid("%s.check.duration: %v", field, err)
			}
			d = o.Check.GetDuration().AsDuration()
		}
		out.Outcome = core.CheckOutcome{Duration: d}
	case *cliv1.Result_Measure:
		dir, err := toDirection(field+".measure.metric.direction", o.Measure.GetMetric().GetDirection())
		if err != nil {
			return nil, err
		}
		m := core.MeasureOutcome{
			Metric:  core.Metric{Name: o.Measure.GetMetric().GetName(), Direction: dir},
			Samples: slices.Clone(o.Measure.GetSamples()), CheckJob: core.JobID(o.Measure.GetCheckJobId()),
		}
		for _, ob := range o.Measure.GetObservations() {
			m.Observations = append(m.Observations, core.Observation{Name: ob.GetName(), Values: slices.Clone(ob.GetValues())})
		}
		out.Outcome = m
	default:
		return nil, invalid("%s: a result says whether it is a check or a measurement", field)
	}
	return &out, nil
}
