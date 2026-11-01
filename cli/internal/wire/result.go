package wire

import (
	"slices"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Every table below pairs a domain value with its contract value and is read in one direction
// only: the daemon sends results, verdicts and promotions and never receives them.

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

var directions = map[core.Direction]cliv1.Direction{
	core.Maximize: cliv1.Direction_DIRECTION_MAXIMIZE,
	core.Minimize: cliv1.Direction_DIRECTION_MINIMIZE,
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

// Result returns the contract's message for a saved result.
func Result(r core.Result) *cliv1.Result {
	out := &cliv1.Result{
		JobId: string(r.Job), Key: string(r.Key), Task: r.Task, TaskVersion: r.TaskVersion,
		Commit: string(r.Commit), SchemaVersion: r.SchemaVersion,
		Environment: Fingerprint(r.Environment),
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
		measured := &cliv1.MeasureResult{
			Metric:  &cliv1.Metric{Name: o.Metric.Name, Direction: directions[o.Metric.Direction]},
			Samples: slices.Clone(o.Samples), CheckJobId: string(o.CheckJob),
		}
		for _, obs := range o.Observations {
			measured.Observations = append(measured.Observations, &cliv1.Observation{Name: obs.Name, Values: slices.Clone(obs.Values)})
		}
		out.Outcome = &cliv1.Result_Measure{Measure: measured}
	}
	return out
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
	case core.Promotion:
		return &cliv1.PromoteResponse{Outcome: &cliv1.PromoteResponse_Promoted{Promoted: Promotion(d)}}
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
