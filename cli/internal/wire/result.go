package wire

import (
	"slices"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A result goes through here from a supervisor to the daemon, as what a check or a measure job
// found, and from the daemon to a client, as what is saved. A verdict, a comparison and a promotion
// only go out: the daemon sends them and never receives them.

var verdictKinds = map[core.VerdictKind]cliv1.VerdictKind{
	core.Better:       cliv1.VerdictKind_VERDICT_KIND_BETTER,
	core.Worse:        cliv1.VerdictKind_VERDICT_KIND_WORSE,
	core.WithinNoise:  cliv1.VerdictKind_VERDICT_KIND_WITHIN_NOISE,
	core.Inconclusive: cliv1.VerdictKind_VERDICT_KIND_INCONCLUSIVE,
}

var inconclusiveReasons = map[core.InconclusiveReason]cliv1.InconclusiveReason{
	core.ReasonTooFewSamples:       cliv1.InconclusiveReason_INCONCLUSIVE_REASON_TOO_FEW_SAMPLES,
	core.ReasonConstantSamples:     cliv1.InconclusiveReason_INCONCLUSIVE_REASON_CONSTANT_SAMPLES,
	core.ReasonMetricMismatch:      cliv1.InconclusiveReason_INCONCLUSIVE_REASON_METRIC_MISMATCH,
	core.ReasonTaskVersionMismatch: cliv1.InconclusiveReason_INCONCLUSIVE_REASON_TASK_VERSION_MISMATCH,
}

var refusalKinds = map[core.PromoteRefusalKind]cliv1.PromoteRefusalKind{
	core.RefusalUnmeasured:          cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_UNMEASURED,
	core.RefusalCheckMissing:        cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_CHECK_MISSING,
	core.RefusalWorse:               cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_WORSE,
	core.RefusalWithinNoise:         cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_WITHIN_NOISE,
	core.RefusalInconclusive:        cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_INCONCLUSIVE,
	core.RefusalStale:               cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_STALE,
	core.RefusalEnvironmentMismatch: cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_ENVIRONMENT_MISMATCH,
}

var noResultReasons = map[core.NoCurrentResultReason]cliv1.NoCurrentResultReason{
	core.NoHead:                    cliv1.NoCurrentResultReason_NO_CURRENT_RESULT_REASON_NO_HEAD,
	core.NotMeasuredSincePromotion: cliv1.NoCurrentResultReason_NO_CURRENT_RESULT_REASON_NOT_MEASURED_SINCE_PROMOTION,
}

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

// Result returns the contract's message for a result.
func Result(r core.Result) *cliv1.Result {
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

// Verdict returns the contract's message for a verdict. Its numbers are carried as they are,
// never rounded: rounding is for text.
func Verdict(v core.Verdict) *cliv1.Verdict {
	return &cliv1.Verdict{
		Kind:             verdictKinds[v.Kind],
		Reason:           inconclusiveReasons[v.Reason],
		CandidateSamples: v.CandidateSamples,
		BaseSamples:      v.BaseSamples,
		CandidateMean:    v.CandidateMean,
		BaseMean:         v.BaseMean,
		Ratio:            v.Ratio,
		Noise:            v.Noise,
	}
}

// Comparison returns the contract's message for a comparison.
func Comparison(c core.Comparison) *cliv1.Comparison {
	return &cliv1.Comparison{
		Commit: string(c.Commit), Against: string(c.Against),
		Verdict: Verdict(c.Verdict), Environment: Fingerprint(c.Environment),
	}
}

// Results returns the contract's messages for saved results.
func Results(rs []core.Result) []*cliv1.Result {
	out := make([]*cliv1.Result, len(rs))
	for i, r := range rs {
		out[i] = Result(r)
	}
	return out
}

// Promotion returns the contract's message for a promotion.
func Promotion(p core.Promotion) *cliv1.Promotion {
	return &cliv1.Promotion{
		Commit: string(p.Commit), PreviousHead: string(p.PreviousHead), Verdict: Verdict(p.Verdict),
		Environment: Fingerprint(p.Environment), ResultJobId: string(p.ResultJob), At: timestamp(p.At),
	}
}

// PromoteRefusal returns the contract's message for a refusal, with every reason and the
// evidence it rests on.
func PromoteRefusal(r core.PromoteRefusal) *cliv1.PromoteRefusal {
	out := &cliv1.PromoteRefusal{}
	for _, reason := range r.Reasons {
		m := &cliv1.PromoteRefusalReason{
			Kind: refusalKinds[reason.Kind], Message: reason.Message,
			MeasuredAgainst: string(reason.MeasuredAgainst), CurrentHead: string(reason.CurrentHead),
		}
		if reason.Verdict != nil {
			m.Verdict = Verdict(*reason.Verdict)
		}
		out.Reasons = append(out.Reasons, m)
	}
	return out
}

// PromoteResponse returns the answer to a promotion: the promotion that happened or the refusal.
func PromoteResponse(d core.PromotionDecision) *cliv1.PromoteResponse {
	switch d := d.(type) {
	case core.Approved:
		return &cliv1.PromoteResponse{Outcome: &cliv1.PromoteResponse_Promoted{Promoted: Promotion(d.Promotion())}}
	case core.PromoteRefusal:
		return &cliv1.PromoteResponse{Outcome: &cliv1.PromoteResponse_Refused{Refused: PromoteRefusal(d)}}
	}
	return &cliv1.PromoteResponse{}
}

// CurrentResultResponse returns the answer to a request for the current result.
func CurrentResultResponse(a core.ResultAnswer) *cliv1.CurrentResultResponse {
	switch a := a.(type) {
	case core.CurrentResult:
		return &cliv1.CurrentResultResponse{State: &cliv1.CurrentResultResponse_Current{
			Current: &cliv1.CurrentResult{Head: string(a.Head), Result: Result(a.Result)},
		}}
	case core.NoCurrentResult:
		return &cliv1.CurrentResultResponse{State: &cliv1.CurrentResultResponse_None{
			None: &cliv1.NoCurrentResult{Reason: noResultReasons[a.Reason], Head: string(a.Head)},
		}}
	}
	return &cliv1.CurrentResultResponse{}
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
