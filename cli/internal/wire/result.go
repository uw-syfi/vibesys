package wire

import (
	"fmt"
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
	core.Tradeoff:     cliv1.VerdictKind_VERDICT_KIND_TRADEOFF,
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
	core.RefusalTradeoff:            cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_TRADEOFF,
}

var noResultReasons = map[core.NoCurrentResultReason]cliv1.NoCurrentResultReason{
	core.NoHead:          cliv1.NoCurrentResultReason_NO_CURRENT_RESULT_REASON_NO_HEAD,
	core.HeadNotMeasured: cliv1.NoCurrentResultReason_NO_CURRENT_RESULT_REASON_HEAD_NOT_MEASURED,
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

var sampleServices = []struct {
	domain core.SampleService
	wire   cliv1.SampleService
}{
	{core.SampleServiceNone, cliv1.SampleService_SAMPLE_SERVICE_UNSPECIFIED},
	{core.SampleServiceStarted, cliv1.SampleService_SAMPLE_SERVICE_STARTED},
	{core.SampleServiceReused, cliv1.SampleService_SAMPLE_SERVICE_REUSED},
	{core.SampleServiceRestarted, cliv1.SampleService_SAMPLE_SERVICE_RESTARTED},
}

func sampleServicesOf(ss []core.SampleService) []cliv1.SampleService {
	if len(ss) == 0 {
		return nil
	}
	out := make([]cliv1.SampleService, len(ss))
	for i, s := range ss {
		for _, p := range sampleServices {
			if p.domain == s {
				out[i] = p.wire
			}
		}
	}
	return out
}

// ToSampleServices reads the servers of a measurement's samples: none, or one per sample, each one
// a sample can name.
func ToSampleServices(field string, ss []cliv1.SampleService, samples int) ([]core.SampleService, error) {
	return toSampleServices(field, ss, samples)
}

func toSampleServices(field string, ss []cliv1.SampleService, samples int) ([]core.SampleService, error) {
	if len(ss) == 0 {
		return nil, nil
	}
	if len(ss) != samples {
		return nil, invalid("%s: %d entries for %d samples", field, len(ss), samples)
	}
	out := make([]core.SampleService, len(ss))
next:
	for i, s := range ss {
		for _, p := range sampleServices {
			if p.wire == s {
				out[i] = p.domain
				continue next
			}
		}
		return nil, invalid("%s[%d]: %s is not a sample's server", field, i, s)
	}
	return out, nil
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
		RecordedAt:     timestamp(r.RecordedAt),
		CandidateImage: r.CandidateImage,
	}
	switch o := r.Outcome.(type) {
	case core.CheckOutcome:
		out.Outcome = &cliv1.Result_Check{Check: &cliv1.CheckResult{Duration: durationpb.New(o.Duration)}}
	case core.MeasureOutcome:
		out.Outcome = &cliv1.Result_Measure{Measure: measureResult(o)}
	}
	return out
}

func measureResult(o core.MeasureOutcome) *cliv1.MeasureResult {
	m := &cliv1.MeasureResult{
		Metric:  &cliv1.Metric{Name: o.Metric.Name, Direction: direction(o.Metric.Direction), Unit: o.Metric.Unit},
		Samples: slices.Clone(o.Samples), CheckJobId: string(o.CheckJob),
		Value: core.Headline(o.Samples), SampleServices: sampleServicesOf(o.Services),
	}
	for _, ob := range o.Observations {
		m.Observations = append(m.Observations, observation(ob))
	}
	m.Objectives = Objectives(o.Objectives)
	return m
}

// observation is the contract's message for an observation, with its headline value, the number
// compare weighs when the observation is an objective; an observation with no values has none.
func observation(ob core.Observation) *cliv1.Observation {
	out := &cliv1.Observation{Name: ob.Name, Values: slices.Clone(ob.Values), Unit: ob.Unit}
	if len(ob.Values) > 0 {
		v := core.Headline(ob.Values)
		out.Value = &v
	}
	return out
}

// Measurements returns the contract's messages for pooled measurements.
func Measurements(ms []core.Measurement) []*cliv1.Measurement {
	out := make([]*cliv1.Measurement, len(ms))
	for i, m := range ms {
		jobs := make([]string, len(m.Jobs))
		for j, job := range m.Jobs {
			jobs[j] = string(job)
		}
		out[i] = &cliv1.Measurement{
			Commit: string(m.Commit), TaskVersion: m.TaskVersion, Environment: Fingerprint(m.Environment),
			JobIds: jobs, Measure: measureResult(m.Outcome),
		}
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

// Objectives returns the contract's message for a measurement's objectives, and nil for the zero
// Objectives of a single-metric task, so that its result is the message it always was.
func Objectives(o core.Objectives) *cliv1.Objectives {
	if len(o.Metrics) == 0 && o.RelativeNoise == 0 && len(o.Noise) == 0 {
		return nil
	}
	out := &cliv1.Objectives{RelativeNoise: o.RelativeNoise}
	for _, n := range o.Noise {
		out.Noise = append(out.Noise, &cliv1.ObjectiveNoise{Name: n.Name, RelativeNoise: n.RelativeNoise})
	}
	for _, m := range o.Metrics {
		out.Metrics = append(out.Metrics, &cliv1.Metric{Name: m.Name, Direction: direction(m.Direction), Unit: m.Unit})
	}
	return out
}

// toObjectives returns the domain objectives for the contract's message, with each direction read
// by toDirection. An absent message is the zero Objectives.
func toObjectives(field string, o *cliv1.Objectives, toDirection func(string, cliv1.Direction) (core.Direction, error)) (core.Objectives, error) {
	if o == nil {
		return core.Objectives{}, nil
	}
	out := core.Objectives{RelativeNoise: o.GetRelativeNoise()}
	for _, n := range o.GetNoise() {
		out.Noise = append(out.Noise, core.ObjectiveNoise{Name: n.GetName(), RelativeNoise: n.GetRelativeNoise()})
	}
	for i, m := range o.GetMetrics() {
		d, err := toDirection(fmt.Sprintf("%s.metrics[%d].direction", field, i), m.GetDirection())
		if err != nil {
			return core.Objectives{}, err
		}
		out.Metrics = append(out.Metrics, core.Metric{Name: m.GetName(), Direction: d, Unit: m.GetUnit()})
	}
	return out, nil
}

// ObjectiveVerdicts returns the contract's messages for the verdicts on a comparison's objectives.
func ObjectiveVerdicts(vs []core.ObjectiveVerdict) []*cliv1.ObjectiveVerdict {
	var out []*cliv1.ObjectiveVerdict
	for _, v := range vs {
		out = append(out, &cliv1.ObjectiveVerdict{
			Metric:  &cliv1.Metric{Name: v.Metric.Name, Direction: direction(v.Metric.Direction), Unit: v.Metric.Unit},
			Verdict: Verdict(v.Verdict),
		})
	}
	return out
}

// Comparison returns the contract's message for a comparison.
func Comparison(c core.Comparison) *cliv1.Comparison {
	return &cliv1.Comparison{
		Commit: string(c.Commit), Against: string(c.Against),
		Verdict: Verdict(c.Verdict), Environment: Fingerprint(c.Environment),
		Objectives: ObjectiveVerdicts(c.Objectives),
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
		Objectives: ObjectiveVerdicts(p.Objectives),
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
			Objectives: ObjectiveVerdicts(reason.Objectives),
		}
		if reason.Verdict != nil {
			m.Verdict = Verdict(*reason.Verdict)
		}
		out.Reasons = append(out.Reasons, m)
	}
	return out
}

// PromoteResponse returns the answer to a promotion: the promotion that happened or the refusal,
// and what the frontier made of a promoted multi-objective commit, which is nil otherwise.
func PromoteResponse(d core.PromotionDecision, frontier core.FrontierDecision) *cliv1.PromoteResponse {
	out := &cliv1.PromoteResponse{}
	switch d := d.(type) {
	case core.Approved:
		out.Outcome = &cliv1.PromoteResponse_Promoted{Promoted: Promotion(d.Promotion())}
	case core.PromoteRefusal:
		out.Outcome = &cliv1.PromoteResponse_Refused{Refused: PromoteRefusal(d)}
	}
	if frontier != nil {
		out.Frontier = FrontierDecision(frontier)
	}
	return out
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
		RecordedAt: toTime(r.GetRecordedAt()), CandidateImage: r.GetCandidateImage(),
	}
	if c := out.CandidateImage; c != "" && !core.ValidImageDigest(c) {
		return core.Result{}, invalid("%s.candidate_image: %q is not an exact image digest", field, c)
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
			Metric:  core.Metric{Name: o.Measure.GetMetric().GetName(), Direction: dir, Unit: o.Measure.GetMetric().GetUnit()},
			Samples: slices.Clone(o.Measure.GetSamples()), CheckJob: core.JobID(o.Measure.GetCheckJobId()),
		}
		for _, ob := range o.Measure.GetObservations() {
			m.Observations = append(m.Observations, core.Observation{Name: ob.GetName(), Values: slices.Clone(ob.GetValues()), Unit: ob.GetUnit()})
		}
		if m.Objectives, err = toObjectives(field+".measure.objectives", o.Measure.GetObjectives(), toDirection); err != nil {
			return core.Result{}, err
		}
		if m.Services, err = toSampleServices(field+".measure.sample_services", o.Measure.GetSampleServices(), len(m.Samples)); err != nil {
			return core.Result{}, err
		}
		out.Outcome = m
	default:
		return core.Result{}, invalid("%s: a result says whether it is a check or a measurement", field)
	}
	return out, nil
}
