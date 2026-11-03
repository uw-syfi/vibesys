package filestore

import (
	"fmt"
	"slices"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/ledger"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// formatVersion is the version of every record and of the layout. A program reads records of
// this version and refuses any other: a newer one is reported as newer, and an older one as one
// this program does not migrate. A change to a record that an older program could not read raises
// it.
//
// Version 1 was a hand-written JSON encoding. Version 2 is protojson of the messages in
// proto/vibesys/cli/v1/store.proto, so the stored form is generated from the contract and not kept
// by hand. A prerelease store of version 1 is thrown away and never converted. Version 3 adds
// Job.submitters, which a program of version 2 refuses as an unknown field; no release wrote version
// 2, so a store of it is thrown away too.
const formatVersion = 3

// The files of a store are the messages of store.proto in protojson: the proto field names, and
// only the fields that are set, so that adding a field to a message does not change the bytes of
// a record that leaves it unset. Decoding refuses an unknown field, which is how a record of a
// different shape is told from one this program wrote.
//
// The output is indented for a person to read. Its exact spacing is not stable: protojson adds a
// space after a colon or comma at random, differently for each build, to keep programs from
// depending on it. Nothing here does: the store compares the owner record only with the bytes it
// wrote itself, and every other file is read back through decode. A test that pins a file compares
// it with its spaces removed.
var (
	encoder = protojson.MarshalOptions{UseProtoNames: true, Multiline: true, Indent: "  "}
	// The first read of a file looks only at its version, so it discards what else the file holds.
	versionReader = protojson.UnmarshalOptions{DiscardUnknown: true}
	recordReader  = protojson.UnmarshalOptions{DiscardUnknown: false}
)

// encode writes a file's content: one JSON document and a newline.
func encode(file proto.Message) ([]byte, error) {
	data, err := encoder.Marshal(file)
	if err != nil {
		return nil, err
	}
	return append(data, '\n'), nil
}

// decode reads a file's content into file, which must be a pointer to one of the file messages. It
// reads the version first and refuses one this program does not read, naming the file, so that a
// newer layout is reported as newer and not as a record with unknown fields. That first read also
// refuses anything after the document. It then refuses unknown fields.
func decode(name string, data []byte, file proto.Message) error {
	var head cliv1.FormatFile
	if err := versionReader.Unmarshal(data, &head); err != nil {
		return corrupt(name, "%v", err)
	}
	switch v := head.GetFormat(); {
	case v == 0:
		return corrupt(name, "it has no format version")
	case v > formatVersion:
		return &core.Failure{Kind: core.FailureIncompatible, Message: fmt.Sprintf(
			"%s is format version %d, and this program reads version %d: upgrade vibesys-cli", name, v, formatVersion)}
	case v < formatVersion:
		return &core.Failure{Kind: core.FailureIncompatible, Message: fmt.Sprintf(
			"%s is format version %d, from a prerelease encoding that this program does not read (it reads version %d): "+
				"remove the store and let the daemon make it again", name, v, formatVersion)}
	}
	if err := recordReader.Unmarshal(data, file); err != nil {
		return corrupt(name, "%v", err)
	}
	return nil
}

func corrupt(name, format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("%s is damaged: %s", name, fmt.Sprintf(format, args...))}
}

// Instants. A time is a Timestamp that is always set, so that a pointer to the zero time and a nil
// pointer stay different; a plain time that is absent from a file is the zero time.

func stamp(t time.Time) *timestamppb.Timestamp { return timestamppb.New(t) }

func stampPtr(t *time.Time) *timestamppb.Timestamp {
	if t == nil {
		return nil
	}
	return stamp(*t)
}

func instant(t *timestamppb.Timestamp) time.Time {
	if t == nil {
		return time.Time{}
	}
	return t.AsTime()
}

func instantPtr(t *timestamppb.Timestamp) *time.Time {
	if t == nil {
		return nil
	}
	v := t.AsTime()
	return &v
}

// Core to message. The encoders of internal/wire do the work wherever they lose nothing: what
// differs here is that the store keeps what the ledger held, so it does not ask for a full commit
// hash or a kind the domain would not accept from a peer.

func jobToMessage(j core.Job) (*cliv1.Job, error) {
	if j.Spec.Settings == nil {
		return nil, fmt.Errorf("job %s: settings of type %T have no encoding", j.ID, j.Spec.Settings)
	}
	out := wire.Job(j)
	out.StartedAt, out.FinishedAt = stampPtr(j.StartedAt), stampPtr(j.FinishedAt)
	out.SubmittedAt = stamp(j.SubmittedAt)
	for i, a := range j.Attempts {
		out.Attempts[i].StartedAt, out.Attempts[i].EndedAt = stamp(a.StartedAt), stamp(a.EndedAt)
	}
	return out, nil
}

func resultToMessage(r core.Result) (*cliv1.Result, error) {
	if r.Outcome == nil {
		return nil, fmt.Errorf("result of job %s: an outcome of type %T has no encoding", r.Job, r.Outcome)
	}
	out := wire.Result(r)
	out.RecordedAt, out.Load.SampledAt = stamp(r.RecordedAt), stamp(r.Load.SampledAt)
	return out, nil
}

func detailToMessage(d core.EventDetail) (*cliv1.Event, error) {
	out := &cliv1.Event{}
	switch d := d.(type) {
	case nil:
	case core.Job:
		job, err := jobToMessage(d)
		out.Detail = &cliv1.Event_Job{Job: job}
		return out, err
	case core.Result:
		result, err := resultToMessage(d)
		out.Detail = &cliv1.Event_Result{Result: result}
		return out, err
	case core.Verdict:
		out.Detail = &cliv1.Event_Verdict{Verdict: wire.Verdict(d)}
	case core.Promotion:
		out.Detail = &cliv1.Event_Promotion{Promotion: wire.Promotion(d)}
	case core.PromoteRefusal:
		out.Detail = &cliv1.Event_Refusal{Refusal: wire.PromoteRefusal(d)}
	case core.Failure:
		out.Detail = &cliv1.Event_Failure{Failure: wire.Failure(d)}
	case core.Plan:
		out.Detail = &cliv1.Event_Plan{Plan: wire.Plan(d)}
	default:
		return nil, fmt.Errorf("an event detail of type %T has no encoding", d)
	}
	return out, nil
}

func eventToMessage(e core.Event) (*cliv1.Event, error) {
	out, err := detailToMessage(e.Detail)
	if err != nil {
		return nil, fmt.Errorf("event %d of run %s: %w", e.Sequence, e.Run, err)
	}
	out.Sequence, out.SchemaVersion, out.RunId, out.HostId, out.Task = e.Sequence, e.SchemaVersion, string(e.Run), e.HostID, e.Task
	out.Commit, out.PlanNumber, out.Harness, out.Agent, out.At = string(e.Commit), uint32(e.Plan), e.Harness, e.Agent, stamp(e.At)
	out.Command, out.Outcome, out.JobId = wire.Command(e.Command), wire.CommandOutcome(e.Outcome), string(e.Job)
	return out, nil
}

// Message to core.

func fingerprintFromMessage(field string, f *cliv1.EnvironmentFingerprint) (core.Fingerprint, error) {
	return wire.ToFingerprint(field, f)
}

func settingsFromMessage(s *cliv1.JobSpec) (core.Settings, error) {
	switch settings := s.GetSettings().(type) {
	case *cliv1.JobSpec_Check:
		return core.CheckSettings{}, nil
	case *cliv1.JobSpec_Command:
		out := core.CommandSettings{Argv: slices.Clone(settings.Command.GetArgv())}
		if svc := settings.Command.GetService(); svc != nil {
			out.Service = &core.ServiceSpec{Argv: slices.Clone(svc.GetArgv()), Probe: slices.Clone(svc.GetProbe())}
		}
		if timeout := settings.Command.GetTimeout(); timeout != nil {
			if err := timeout.CheckValid(); err != nil {
				return nil, fmt.Errorf("command timeout: %v", err)
			}
			out.Timeout = timeout.AsDuration()
		}
		return out, nil
	case *cliv1.JobSpec_Measure:
		return core.MeasureSettings{Samples: settings.Measure.GetSamples(), Recheck: settings.Measure.GetRecheck()}, nil
	}
	return nil, fmt.Errorf("a spec with no settings")
}

func jobFromMessage(m *cliv1.Job) (core.Job, error) {
	fail := func(err error) (core.Job, error) { return core.Job{}, fmt.Errorf("job %s: %w", m.GetId(), err) }
	settings, err := settingsFromMessage(m.GetSpec())
	if err != nil {
		return fail(err)
	}
	env, err := wire.ToEnvironmentKind("spec.environment", m.GetSpec().GetEnvironment())
	if err != nil {
		return fail(err)
	}
	state, err := wire.ToJobState("state", m.GetState())
	if err != nil {
		return fail(err)
	}
	j := core.Job{
		ID: core.JobID(m.GetId()), Spec: core.JobSpec{Commit: core.Commit(m.GetSpec().GetCommit()), Environment: env, Settings: settings},
		State: state, Key: core.JobKey(m.GetKey()), NoCache: m.GetNoCache(),
		SubmittedAt: instant(m.GetSubmittedAt()), StartedAt: instantPtr(m.GetStartedAt()), FinishedAt: instantPtr(m.GetFinishedAt()),
		CancelReason: m.GetCancelReason(), ResultJob: core.JobID(m.GetResultJobId()), CheckJob: core.JobID(m.GetCheckJobId()),
	}
	if m.GetEnvironment() != nil {
		fp, err := fingerprintFromMessage("environment", m.GetEnvironment())
		if err != nil {
			return fail(err)
		}
		j.Environment = &fp
	}
	for _, n := range m.GetNotes() {
		j.Notes = append(j.Notes, core.Note{Text: n.GetText(), At: instant(n.GetAt()), Agent: n.GetAgent()})
	}
	for _, p := range m.GetPlans() {
		j.Plans = append(j.Plans, core.PlanNumber(p))
	}
	if m.ExitCode != nil {
		code := int(m.GetExitCode())
		j.ExitCode = &code
	}
	if n := len(m.GetAttempts()); n >= core.MaxAttempts {
		return fail(fmt.Errorf("%d attempts cut off, and a job gets %d in all", n, core.MaxAttempts))
	}
	for i, a := range m.GetAttempts() {
		past, err := wire.ToAttempt(fmt.Sprintf("attempts[%d]", i), a)
		if err != nil {
			return fail(err)
		}
		past.StartedAt, past.EndedAt = instant(a.GetStartedAt()), instant(a.GetEndedAt())
		j.Attempts = append(j.Attempts, past)
	}
	for _, sub := range m.GetSubmitters() {
		j.Submitters = append(j.Submitters, wire.ToSubmitter(sub))
	}
	return j, nil
}

func resultFromMessage(m *cliv1.Result) (core.Result, error) {
	fail := func(err error) (core.Result, error) {
		return core.Result{}, fmt.Errorf("result of job %s: %w", m.GetJobId(), err)
	}
	env, err := fingerprintFromMessage("environment", m.GetEnvironment())
	if err != nil {
		return fail(err)
	}
	r := core.Result{
		Job: core.JobID(m.GetJobId()), Key: core.JobKey(m.GetKey()), Task: m.GetTask(), TaskVersion: m.GetTaskVersion(),
		Commit: core.Commit(m.GetCommit()), SchemaVersion: m.GetSchemaVersion(), Environment: env,
		Machine: core.MachineFacts{
			HostID: m.GetMachine().GetHostId(), OS: m.GetMachine().GetOs(), Arch: m.GetMachine().GetArch(), CPUModel: m.GetMachine().GetCpuModel(),
			LogicalCPUs: m.GetMachine().GetLogicalCpus(), MemoryBytes: m.GetMachine().GetMemoryBytes(),
		},
		Load: core.MachineLoad{
			SampledAt: instant(m.GetLoad().GetSampledAt()), LoadAverage1m: m.GetLoad().GetLoadAverage_1M(),
			CPUBusyFraction: m.GetLoad().GetCpuBusyFraction(), MemoryAvailableBytes: m.GetLoad().GetMemoryAvailableBytes(),
		},
		RecordedAt: instant(m.GetRecordedAt()),
	}
	switch o := m.GetOutcome().(type) {
	case *cliv1.Result_Check:
		var d time.Duration
		if o.Check.GetDuration() != nil {
			if err := o.Check.GetDuration().CheckValid(); err != nil {
				return fail(fmt.Errorf("check duration: %v", err))
			}
			d = o.Check.GetDuration().AsDuration()
		}
		r.Outcome = core.CheckOutcome{Duration: d}
	case *cliv1.Result_Measure:
		dir, err := wire.ToDirection("metric.direction", o.Measure.GetMetric().GetDirection())
		if err != nil {
			return fail(err)
		}
		out := core.MeasureOutcome{
			Metric:  core.Metric{Name: o.Measure.GetMetric().GetName(), Direction: dir},
			Samples: slices.Clone(o.Measure.GetSamples()), CheckJob: core.JobID(o.Measure.GetCheckJobId()),
		}
		// The headline is derived from the samples and stored beside them for readers of the file; a
		// file whose two disagree has been edited, and is refused.
		if o.Measure.GetValue() != core.Headline(out.Samples) {
			return fail(fmt.Errorf("its headline value %v is not that of its samples", o.Measure.GetValue()))
		}
		for _, ob := range o.Measure.GetObservations() {
			out.Observations = append(out.Observations, core.Observation{Name: ob.GetName(), Values: slices.Clone(ob.GetValues())})
		}
		r.Outcome = out
	default:
		return fail(fmt.Errorf("it says neither that it is a check nor a measurement"))
	}
	return r, nil
}

func verdictFromMessage(field string, v *cliv1.Verdict) (core.Verdict, error) {
	kind, err := wire.ToVerdictKind(field+".kind", v.GetKind())
	if err != nil {
		return core.Verdict{}, err
	}
	reason, err := wire.ToInconclusiveReason(field+".reason", v.GetReason())
	if err != nil {
		return core.Verdict{}, err
	}
	return core.Verdict{
		Kind: kind, Reason: reason, CandidateSamples: v.GetCandidateSamples(), BaseSamples: v.GetBaseSamples(),
		CandidateMean: v.GetCandidateMean(), BaseMean: v.GetBaseMean(), Ratio: v.GetRatio(), Noise: v.GetNoise(),
	}, nil
}

func promotionFromMessage(m *cliv1.Promotion) (core.Promotion, error) {
	verdict, err := verdictFromMessage("promotion.verdict", m.GetVerdict())
	if err != nil {
		return core.Promotion{}, err
	}
	env, err := fingerprintFromMessage("promotion.environment", m.GetEnvironment())
	if err != nil {
		return core.Promotion{}, err
	}
	return core.Promotion{
		Commit: core.Commit(m.GetCommit()), PreviousHead: core.Commit(m.GetPreviousHead()), Verdict: verdict,
		Environment: env, ResultJob: core.JobID(m.GetResultJobId()), At: instant(m.GetAt()),
	}, nil
}

func refusalFromMessage(m *cliv1.PromoteRefusal) (core.PromoteRefusal, error) {
	out := core.PromoteRefusal{}
	for i, rr := range m.GetReasons() {
		field := fmt.Sprintf("refusal.reasons[%d]", i)
		kind, err := wire.ToPromoteRefusalKind(field+".kind", rr.GetKind())
		if err != nil {
			return core.PromoteRefusal{}, err
		}
		reason := core.PromoteRefusalReason{
			Kind: kind, Message: rr.GetMessage(), MeasuredAgainst: core.Commit(rr.GetMeasuredAgainst()), CurrentHead: core.Commit(rr.GetCurrentHead()),
		}
		if rr.GetVerdict() != nil {
			v, err := verdictFromMessage(field+".verdict", rr.GetVerdict())
			if err != nil {
				return core.PromoteRefusal{}, err
			}
			reason.Verdict = &v
		}
		out.Reasons = append(out.Reasons, reason)
	}
	return out, nil
}

func planFromMessage(m *cliv1.Plan) (core.Plan, error) {
	status, err := wire.ToPlanStatus("plan.status", m.GetStatus())
	if err != nil {
		return core.Plan{}, err
	}
	p := core.Plan{
		Number: core.PlanNumber(m.GetNumber()), Title: m.GetTitle(), Body: m.GetBody(), Status: status,
		CreatedAt: instant(m.GetCreatedAt()), UpdatedAt: instant(m.GetUpdatedAt()),
	}
	for _, c := range m.GetComments() {
		p.Comments = append(p.Comments, core.PlanComment{Text: c.GetText(), At: instant(c.GetAt()), Agent: c.GetAgent()})
	}
	return p, nil
}

func detailFromMessage(m *cliv1.Event) (core.EventDetail, error) {
	switch d := m.GetDetail().(type) {
	case nil:
		return nil, nil
	case *cliv1.Event_Job:
		return jobFromMessage(d.Job)
	case *cliv1.Event_Result:
		return resultFromMessage(d.Result)
	case *cliv1.Event_Verdict:
		return verdictFromMessage("verdict", d.Verdict)
	case *cliv1.Event_Promotion:
		return promotionFromMessage(d.Promotion)
	case *cliv1.Event_Refusal:
		return refusalFromMessage(d.Refusal)
	case *cliv1.Event_Failure:
		return wire.ToFailure(d.Failure), nil
	case *cliv1.Event_Plan:
		return planFromMessage(d.Plan)
	}
	return nil, fmt.Errorf("an event detail of type %T", m.GetDetail())
}

func eventFromMessage(m *cliv1.Event) (core.Event, error) {
	fail := func(err error) (core.Event, error) {
		return core.Event{}, fmt.Errorf("event %d of run %s: %w", m.GetSequence(), m.GetRunId(), err)
	}
	command, err := wire.ToCommand("command", m.GetCommand())
	if err != nil {
		return fail(err)
	}
	outcome, err := wire.ToCommandOutcome("outcome", m.GetOutcome())
	if err != nil {
		return fail(err)
	}
	detail, err := detailFromMessage(m)
	if err != nil {
		return fail(err)
	}
	return core.Event{Sequence: m.GetSequence(), EventDraft: core.EventDraft{
		SchemaVersion: m.GetSchemaVersion(), Run: core.RunID(m.GetRunId()), HostID: m.GetHostId(), Task: m.GetTask(), Commit: core.Commit(m.GetCommit()),
		Plan: core.PlanNumber(m.GetPlanNumber()), Harness: m.GetHarness(), Agent: m.GetAgent(), At: instant(m.GetAt()),
		Command: command, Outcome: outcome, Job: core.JobID(m.GetJobId()), Detail: detail,
	}}, nil
}

// savedToFile encodes a result with its place in the order results were saved.
func savedToFile(s ledger.SavedResult) (*cliv1.ResultFile, error) {
	r, err := resultToMessage(s.Result)
	return &cliv1.ResultFile{Format: formatVersion, Seq: s.Seq, Result: r}, err
}
