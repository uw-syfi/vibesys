package filestore

import (
	"errors"
	"fmt"
	"slices"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
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
// by hand. A prerelease store of version 1 is thrown away and never converted. A job's submitters
// were added within version 2: the field is additive and absent on historical jobs, and attribution
// is never invented for records written before submitters existed.
//
// Version 3 adds the frontier/ directory, and fields that a version 2 program would refuse as
// unknown: a measurement's objectives, and the verdict on each objective of a promotion and of a
// refusal. It only adds, so a record of version 2 is a record of version 3 as it is: this program
// reads both, and migrates a store of version 2 by making the frontier directory and then writing
// the format file at version 3, which a program of version 2 refuses to open from then on. The
// records keep the version they were written with.
//
// Version 4 adds the placement of a job's spec, which a version 3 program would refuse as unknown.
// It only adds, so this program reads versions 2 to 4 and migrates a store of 2 or 3 the same way,
// writing the format file at version 4 last.
//
// Version 5 adds a job's needs and the task version it was submitted for, which a version 4
// program would refuse as unknown, and comes with a
// new rule for the cache key: a docker environment's facts, among them the placement it ran on, are
// no longer part of it (core.Fingerprint.Equal), since placements are interchangeable. A result of a
// record of version 4 or older was keyed under the old rule, so its key is derived again when it is
// read (rekeyed), and so is the key of the job that saved it; the record itself is not rewritten,
// and keeps the version it was written with. Results of one image taken on two placements, which
// had two keys, then answer the same key. The migration of a store of 2 to 4 is the one before,
// writing the format file at version 5 last.
//
// Version 6 adds a plan's origin, status transitions and workstream, which a version 5 program would
// refuse as unknown, and the directories of the records that came with them: workstreams/, and
// bounds/, which the version 5 program that first wrote declared bounds made when it wrote one and
// read as empty when there was none. It only adds, so this program reads versions 2 to 6, and Open
// migrates a store of 2 to 5 the same way: it makes every directory the store lacks, and writes the
// format file at version 6 last. A program of version 5 refuses the store from then on, which is
// what keeps it from opening a store whose bounds or workstreams it would not see and ignoring them.
// A plan saved before has no origin and no transitions, and none is invented for it.
//
// Version 7 adds the standing allocation of a job's spec (JobSpec.allocation), which a version 6
// program would refuse as unknown, with the per-daemon allocation log and identity that come with
// it. It only adds, so this program reads versions 2 to 7, and Open migrates a store of 2 to 6 the
// same way, writing the format file at version 7 last, so that a program of version 6 refuses the
// store instead of failing on the first job of an allocation it reads.
const formatVersion = 7

// rekeyedBefore is the first version whose results carry keys of the current rule: a result of a
// record older than it is rekeyed when it is read.
const rekeyedBefore = 5

// oldestFormat is the oldest version this program reads, and migrates a store of.
const oldestFormat = 2

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
	if err := wire.Validate(file); err != nil {
		return nil, err
	}
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
	case v < oldestFormat:
		return store.Mark(&core.Failure{Kind: core.FailureIncompatible, Message: fmt.Sprintf(
			"%s is format version %d, from a prerelease encoding that this program does not read (it reads versions %d to %d): "+
				"%s", name, v, oldestFormat, formatVersion, prereleaseFix)}, errPrerelease)
	}
	if err := recordReader.Unmarshal(data, file); err != nil {
		return corrupt(name, "%v", err)
	}
	if err := wire.Validate(file); err != nil {
		return corrupt(name, "%v", err)
	}
	return nil
}

// errPrerelease marks a refusal of a file of a version older than oldestFormat, which Open completes
// with what to remove.
var errPrerelease = errors.New("a prerelease encoding")

// prereleaseFix ends the refusal of a file of a prerelease encoding where Open cannot say more.
const prereleaseFix = "remove the store and let the daemon make it again"

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
		// A job stored before the profile job was retired holds its resolved command beside the
		// request, which is not read.
		var err error
		if out.Image, err = wire.ToCommandImage("command.image", settings.Command.GetImage()); err != nil {
			return nil, err
		}
		out.Wrap = slices.Clone(settings.Command.GetWrap())
		if out.Candidate, err = wire.ToCommandCandidate("command.candidate", settings.Command.GetCandidate()); err != nil {
			return nil, err
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
		ID: core.JobID(m.GetId()), Spec: core.JobSpec{Commit: core.Commit(m.GetSpec().GetCommit()), Environment: env, Placement: m.GetSpec().GetPlacement(), Allocation: m.GetSpec().GetAllocation(), Settings: settings},
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
		if err := sub.GetAt().CheckValid(); err != nil {
			return fail(fmt.Errorf("submitter.at: %w", err))
		}
		j.Submitters = append(j.Submitters, wire.ToSubmitter(sub))
	}
	for i, k := range m.GetNeeds() {
		kind, err := wire.ToResourceKind(fmt.Sprintf("needs[%d]", i), k)
		if err != nil {
			return fail(err)
		}
		j.Needs = append(j.Needs, kind)
	}
	j.TaskVersion = m.GetTaskVersion()
	if j.FailedStage, err = wire.ToJobStage("failed_stage", m.GetFailedStage()); err != nil {
		return fail(err)
	}
	return j, nil
}

// rekeyed is a result of a record older than rekeyedBefore with the key the current rule gives it:
// that of its kind, commit, task version, environment and schema version, the same inputs it was
// keyed by when it was saved. Only a docker result's key changes, as the rule leaves its
// environment's facts out now; any other keeps its own. A result whose key the rule cannot give, such as
// one of an environment that is not pinned, keeps the key it has.
func rekeyed(r core.Result) core.Result {
	if r.Environment.Kind != core.EnvironmentDocker {
		return r
	}
	kind, settings := core.JobKind(core.JobCheck), core.Settings(core.CheckSettings{})
	if _, ok := r.Outcome.(core.MeasureOutcome); ok {
		kind, settings = core.JobMeasure, core.MeasureSettings{}
	}
	key, err := core.NewJobKey(core.KeyInputs{
		Kind: kind, Commit: r.Commit, TaskVersion: r.TaskVersion, Environment: r.Environment, Settings: settings, SchemaVersion: r.SchemaVersion,
	})
	if err == nil {
		r.Key = key
	}
	return r
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
		RecordedAt: instant(m.GetRecordedAt()), CandidateImage: m.GetCandidateImage(),
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
			Metric:  core.Metric{Name: o.Measure.GetMetric().GetName(), Direction: dir, Unit: o.Measure.GetMetric().GetUnit()},
			Samples: slices.Clone(o.Measure.GetSamples()), CheckJob: core.JobID(o.Measure.GetCheckJobId()),
		}
		// The headline is derived from the samples and stored beside them for readers of the file; a
		// file whose two disagree has been edited, and is refused.
		if o.Measure.GetValue() != core.Headline(out.Samples) {
			return fail(fmt.Errorf("its headline value %v is not that of its samples", o.Measure.GetValue()))
		}
		for _, ob := range o.Measure.GetObservations() {
			values := slices.Clone(ob.GetValues())
			// An observation's headline is stored the same way, by builds that know it.
			if ob.Value != nil && (len(values) == 0 || ob.GetValue() != core.Headline(values)) {
				return fail(fmt.Errorf("the headline value %v of its observation %q is not that of its values", ob.GetValue(), ob.GetName()))
			}
			out.Observations = append(out.Observations, core.Observation{Name: ob.GetName(), Values: values, Unit: ob.GetUnit()})
		}
		if out.Objectives, err = objectivesFromMessage("objectives", o.Measure.GetObjectives()); err != nil {
			return fail(err)
		}
		if out.Services, err = wire.ToSampleServices("sample_services", o.Measure.GetSampleServices(), len(out.Samples)); err != nil {
			return fail(err)
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

// objectivesFromMessage reads a measurement's objectives, which are absent for a single-metric task.
func objectivesFromMessage(field string, o *cliv1.Objectives) (core.Objectives, error) {
	if o == nil {
		return core.Objectives{}, nil
	}
	out := core.Objectives{RelativeNoise: o.GetRelativeNoise()}
	for _, n := range o.GetNoise() {
		out.Noise = append(out.Noise, core.ObjectiveNoise{Name: n.GetName(), RelativeNoise: n.GetRelativeNoise()})
	}
	for i, m := range o.GetMetrics() {
		d, err := wire.ToDirection(fmt.Sprintf("%s.metrics[%d].direction", field, i), m.GetDirection())
		if err != nil {
			return core.Objectives{}, err
		}
		out.Metrics = append(out.Metrics, core.Metric{Name: m.GetName(), Direction: d, Unit: m.GetUnit()})
	}
	return out, nil
}

func objectiveVerdictsFromMessage(field string, vs []*cliv1.ObjectiveVerdict) ([]core.ObjectiveVerdict, error) {
	var out []core.ObjectiveVerdict
	for i, v := range vs {
		at := fmt.Sprintf("%s[%d]", field, i)
		d, err := wire.ToDirection(at+".metric.direction", v.GetMetric().GetDirection())
		if err != nil {
			return nil, err
		}
		verdict, err := verdictFromMessage(at+".verdict", v.GetVerdict())
		if err != nil {
			return nil, err
		}
		out = append(out, core.ObjectiveVerdict{Metric: core.Metric{Name: v.GetMetric().GetName(), Direction: d, Unit: v.GetMetric().GetUnit()}, Verdict: verdict})
	}
	return out, nil
}

func frontierFromFile(f *cliv1.FrontierFile) core.Frontier {
	out := core.Frontier{Revision: f.GetSeq()}
	for _, m := range f.GetMembers() {
		out.Members = append(out.Members, core.FrontierMember{Commit: core.Commit(m.GetCommit()), AddedAt: instant(m.GetAddedAt())})
	}
	return out
}

func frontierToFile(f core.Frontier) *cliv1.FrontierFile {
	out := &cliv1.FrontierFile{Format: formatVersion, Seq: f.Revision}
	for _, m := range f.Members {
		out.Members = append(out.Members, &cliv1.FrontierMember{Commit: string(m.Commit), AddedAt: stamp(m.AddedAt)})
	}
	return out
}

func promotionFromMessage(m *cliv1.Promotion) (core.Promotion, error) {
	verdict, err := verdictFromMessage("promotion.verdict", m.GetVerdict())
	if err != nil {
		return core.Promotion{}, err
	}
	objectives, err := objectiveVerdictsFromMessage("promotion.objectives", m.GetObjectives())
	if err != nil {
		return core.Promotion{}, err
	}
	env, err := fingerprintFromMessage("promotion.environment", m.GetEnvironment())
	if err != nil {
		return core.Promotion{}, err
	}
	return core.Promotion{
		Commit: core.Commit(m.GetCommit()), PreviousHead: core.Commit(m.GetPreviousHead()), Verdict: verdict,
		Objectives: objectives, Environment: env, ResultJob: core.JobID(m.GetResultJobId()), At: instant(m.GetAt()),
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
		if reason.Objectives, err = objectiveVerdictsFromMessage(field+".objectives", rr.GetObjectives()); err != nil {
			return core.PromoteRefusal{}, err
		}
		out.Reasons = append(out.Reasons, reason)
	}
	return out, nil
}

// planFromMessage is the plan a message holds. Every field of a plan is read where the contract is
// read, so a field added to it is read for the store and for the wire alike.
func planFromMessage(m *cliv1.Plan) (core.Plan, error) { return wire.ToPlan(m) }

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
