package wire

import (
	"slices"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A result goes through here only from a supervisor to the daemon: it is what a check or a measure
// job found.

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

// result returns the contract's message for the result a job found.
func result(r core.Result) *cliv1.Result {
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

// toResult returns the domain result for the contract's message. A message with neither outcome is
// refused: a result always says what it measured.
func toResult(field string, r *cliv1.Result) (core.Result, error) {
	env, err := ToFingerprint(field+".environment", r.GetEnvironment())
	if err != nil {
		return core.Result{}, err
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
				return core.Result{}, invalid("%s.check.duration: %v", field, err)
			}
			d = o.Check.GetDuration().AsDuration()
		}
		out.Outcome = core.CheckOutcome{Duration: d}
	case *cliv1.Result_Measure:
		dir, err := toDirection(field+".measure.metric.direction", o.Measure.GetMetric().GetDirection())
		if err != nil {
			return core.Result{}, err
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
		return core.Result{}, invalid("%s: a result says whether it is a check or a measurement", field)
	}
	return out, nil
}
