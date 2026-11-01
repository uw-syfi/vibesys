package filestore

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/ledger"
)

// formatVersion is the version of every record and of the layout. A program reads records of
// this version and refuses a newer one; a change to a record that an older program could not
// read raises it, and the code that reads the old version stays beside the new.
const formatVersion = 1

// The types in this file are the encoding of the store. They are not the core types: renaming a
// core field must not change what is on disk, so every field is copied by name, enumerations
// are plain strings, and a union is a record with a kind and the one body that kind has.

type fingerprintRec struct {
	Kind        string            `json:"kind"`
	Backend     string            `json:"backend"`
	ImageDigest string            `json:"image_digest"`
	Facts       map[string]string `json:"facts"`
}

type machineRec struct {
	HostID      string `json:"host_id"`
	OS          string `json:"os"`
	Arch        string `json:"arch"`
	CPUModel    string `json:"cpu_model"`
	LogicalCPUs uint32 `json:"logical_cpus"`
	MemoryBytes uint64 `json:"memory_bytes"`
}

type loadRec struct {
	SampledAt            time.Time `json:"sampled_at"`
	LoadAverage1m        float64   `json:"load_average_1m"`
	CPUBusyFraction      float64   `json:"cpu_busy_fraction"`
	MemoryAvailableBytes uint64    `json:"memory_available_bytes"`
}

type noteRec struct {
	Text  string    `json:"text"`
	At    time.Time `json:"at"`
	Agent string    `json:"agent"`
}

type serviceRec struct {
	Argv  []string `json:"argv"`
	Probe []string `json:"probe"`
}

type commandSettingsRec struct {
	Argv      []string    `json:"argv"`
	Service   *serviceRec `json:"service"`
	TimeoutNS int64       `json:"timeout_ns"`
}

type measureSettingsRec struct {
	Samples uint32 `json:"samples"`
	Recheck bool   `json:"recheck"`
}

// settingsRec holds the body of the kind it names and no other.
type settingsRec struct {
	Kind    string              `json:"kind"`
	Command *commandSettingsRec `json:"command,omitempty"`
	Measure *measureSettingsRec `json:"measure,omitempty"`
}

type specRec struct {
	Commit      string      `json:"commit"`
	Environment string      `json:"environment"`
	Settings    settingsRec `json:"settings"`
}

type jobRec struct {
	ID           string          `json:"id"`
	Spec         specRec         `json:"spec"`
	State        string          `json:"state"`
	Key          string          `json:"key"`
	Environment  *fingerprintRec `json:"environment"`
	Notes        []noteRec       `json:"notes"`
	Plans        []uint32        `json:"plans"`
	SubmittedAt  time.Time       `json:"submitted_at"`
	StartedAt    *time.Time      `json:"started_at"`
	FinishedAt   *time.Time      `json:"finished_at"`
	CancelReason string          `json:"cancel_reason"`
	ExitCode     *int            `json:"exit_code"`
	ResultJob    string          `json:"result_job"`
	CheckJob     string          `json:"check_job"`
}

type observationRec struct {
	Name   string    `json:"name"`
	Values []float64 `json:"values"`
}

type metricRec struct {
	Name      string `json:"name"`
	Direction string `json:"direction"`
}

type checkOutcomeRec struct {
	DurationNS int64 `json:"duration_ns"`
}

type measureOutcomeRec struct {
	Metric       metricRec        `json:"metric"`
	Samples      []float64        `json:"samples"`
	Observations []observationRec `json:"observations"`
	CheckJob     string           `json:"check_job"`
}

type outcomeRec struct {
	Kind    string             `json:"kind"`
	Check   *checkOutcomeRec   `json:"check,omitempty"`
	Measure *measureOutcomeRec `json:"measure,omitempty"`
}

type resultRec struct {
	Job           string         `json:"job"`
	Key           string         `json:"key"`
	Task          string         `json:"task"`
	TaskVersion   string         `json:"task_version"`
	Commit        string         `json:"commit"`
	SchemaVersion uint32         `json:"schema_version"`
	Environment   fingerprintRec `json:"environment"`
	Machine       machineRec     `json:"machine"`
	Load          loadRec        `json:"load"`
	RecordedAt    time.Time      `json:"recorded_at"`
	Outcome       outcomeRec     `json:"outcome"`
}

type verdictRec struct {
	Kind             string  `json:"kind"`
	Reason           string  `json:"reason"`
	CandidateSamples uint32  `json:"candidate_samples"`
	BaseSamples      uint32  `json:"base_samples"`
	CandidateMean    float64 `json:"candidate_mean"`
	BaseMean         float64 `json:"base_mean"`
	Ratio            float64 `json:"ratio"`
	Noise            float64 `json:"noise"`
}

type promotionRec struct {
	Commit       string         `json:"commit"`
	PreviousHead string         `json:"previous_head"`
	Verdict      verdictRec     `json:"verdict"`
	Environment  fingerprintRec `json:"environment"`
	ResultJob    string         `json:"result_job"`
	At           time.Time      `json:"at"`
}

type refusalReasonRec struct {
	Kind            string      `json:"kind"`
	Message         string      `json:"message"`
	Verdict         *verdictRec `json:"verdict"`
	MeasuredAgainst string      `json:"measured_against"`
	CurrentHead     string      `json:"current_head"`
}

type refusalRec struct {
	Reasons []refusalReasonRec `json:"reasons"`
}

type failureRec struct {
	Kind    string `json:"kind"`
	Message string `json:"message"`
}

type commentRec struct {
	Text  string    `json:"text"`
	At    time.Time `json:"at"`
	Agent string    `json:"agent"`
}

type planRec struct {
	Number    uint32       `json:"number"`
	Title     string       `json:"title"`
	Body      string       `json:"body"`
	Status    string       `json:"status"`
	Comments  []commentRec `json:"comments"`
	CreatedAt time.Time    `json:"created_at"`
	UpdatedAt time.Time    `json:"updated_at"`
}

// detailRec holds the body of the kind it names and no other.
type detailRec struct {
	Kind      string        `json:"kind"`
	Job       *jobRec       `json:"job,omitempty"`
	Result    *resultRec    `json:"result,omitempty"`
	Verdict   *verdictRec   `json:"verdict,omitempty"`
	Promotion *promotionRec `json:"promotion,omitempty"`
	Refusal   *refusalRec   `json:"refusal,omitempty"`
	Failure   *failureRec   `json:"failure,omitempty"`
	Plan      *planRec      `json:"plan,omitempty"`
}

type eventRec struct {
	Sequence      uint64     `json:"sequence"`
	SchemaVersion uint32     `json:"schema_version"`
	Run           string     `json:"run"`
	HostID        string     `json:"host_id"`
	Task          string     `json:"task"`
	Commit        string     `json:"commit"`
	Plan          uint32     `json:"plan"`
	Harness       string     `json:"harness"`
	Agent         string     `json:"agent"`
	At            time.Time  `json:"at"`
	Command       string     `json:"command"`
	Outcome       string     `json:"outcome"`
	Job           string     `json:"job"`
	Detail        *detailRec `json:"detail"`
}

// The files. Each starts with the format version of its content.

type rootFile struct {
	Format int `json:"format"`
}

type jobFile struct {
	Format int    `json:"format"`
	Job    jobRec `json:"job"`
}

type resultFile struct {
	Format int       `json:"format"`
	Seq    uint64    `json:"seq"`
	Result resultRec `json:"result"`
}

type planFile struct {
	Format int     `json:"format"`
	Plan   planRec `json:"plan"`
}

type promotionFile struct {
	Format    int          `json:"format"`
	Seq       uint64       `json:"seq"`
	Promotion promotionRec `json:"promotion"`
}

type eventFile struct {
	Format int      `json:"format"`
	Event  eventRec `json:"event"`
}

// encode writes a file's content: one JSON document and a newline.
func encode(file any) ([]byte, error) {
	data, err := json.Marshal(file)
	if err != nil {
		return nil, err
	}
	return append(data, '\n'), nil
}

// decode reads a file's content into file, which must be a pointer to one of the file types. It
// refuses a version this program does not read, naming the file, before it looks at anything
// else, so that a newer layout is reported as newer and not as a record with unknown fields. That
// first read also refuses anything after the document. It then refuses unknown fields.
func decode(name string, data []byte, file any) error {
	var head struct {
		Format *int `json:"format"`
	}
	if err := json.Unmarshal(data, &head); err != nil {
		return corrupt(name, "%v", err)
	}
	switch {
	case head.Format == nil:
		return corrupt(name, "it has no format version")
	case *head.Format > formatVersion:
		return &core.Failure{Kind: core.FailureIncompatible, Message: fmt.Sprintf(
			"%s is format version %d, and this program reads up to version %d: upgrade vibesys-cli", name, *head.Format, formatVersion)}
	case *head.Format < 1:
		return corrupt(name, "its format version %d is not one this program knows", *head.Format)
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(file); err != nil {
		return corrupt(name, "%v", err)
	}
	return nil
}

func corrupt(name, format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("%s is damaged: %s", name, fmt.Sprintf(format, args...))}
}

// Core to record.

func fingerprintToRec(f core.Fingerprint) fingerprintRec {
	return fingerprintRec{Kind: string(f.Kind), Backend: string(f.Backend), ImageDigest: f.ImageDigest, Facts: f.Facts}
}

func fingerprintFromRec(r fingerprintRec) core.Fingerprint {
	return core.Fingerprint{Kind: core.EnvironmentKind(r.Kind), Backend: core.BackendKind(r.Backend), ImageDigest: r.ImageDigest, Facts: r.Facts}
}

func fingerprintPtrToRec(f *core.Fingerprint) *fingerprintRec {
	if f == nil {
		return nil
	}
	r := fingerprintToRec(*f)
	return &r
}

func fingerprintPtrFromRec(r *fingerprintRec) *core.Fingerprint {
	if r == nil {
		return nil
	}
	f := fingerprintFromRec(*r)
	return &f
}

func settingsToRec(s core.Settings) (settingsRec, error) {
	switch s := s.(type) {
	case core.CheckSettings:
		return settingsRec{Kind: string(core.JobCheck)}, nil
	case core.CommandSettings:
		body := &commandSettingsRec{Argv: s.Argv, TimeoutNS: int64(s.Timeout)}
		if s.Service != nil {
			body.Service = &serviceRec{Argv: s.Service.Argv, Probe: s.Service.Probe}
		}
		return settingsRec{Kind: string(core.JobCommand), Command: body}, nil
	case core.MeasureSettings:
		return settingsRec{Kind: string(core.JobMeasure), Measure: &measureSettingsRec{Samples: s.Samples, Recheck: s.Recheck}}, nil
	}
	return settingsRec{}, fmt.Errorf("settings of type %T have no encoding", s)
}

func settingsFromRec(r settingsRec) (core.Settings, error) {
	bodies := 0
	for _, present := range []bool{r.Command != nil, r.Measure != nil} {
		if present {
			bodies++
		}
	}
	want := map[string]int{string(core.JobCheck): 0, string(core.JobCommand): 1, string(core.JobMeasure): 1}
	if n, known := want[r.Kind]; !known || n != bodies {
		return nil, fmt.Errorf("settings of kind %q with %d bodies", r.Kind, bodies)
	}
	switch core.JobKind(r.Kind) {
	case core.JobCommand:
		if r.Command == nil {
			return nil, errors.New("command settings without a command body")
		}
		s := core.CommandSettings{Argv: r.Command.Argv, Timeout: time.Duration(r.Command.TimeoutNS)}
		if r.Command.Service != nil {
			s.Service = &core.ServiceSpec{Argv: r.Command.Service.Argv, Probe: r.Command.Service.Probe}
		}
		return s, nil
	case core.JobMeasure:
		if r.Measure == nil {
			return nil, errors.New("measure settings without a measure body")
		}
		return core.MeasureSettings{Samples: r.Measure.Samples, Recheck: r.Measure.Recheck}, nil
	case core.JobCheck:
		return core.CheckSettings{}, nil
	}
	return nil, fmt.Errorf("settings of kind %q", r.Kind)
}

func jobToRec(j core.Job) (jobRec, error) {
	settings, err := settingsToRec(j.Spec.Settings)
	if err != nil {
		return jobRec{}, fmt.Errorf("job %s: %w", j.ID, err)
	}
	r := jobRec{
		ID: string(j.ID), Spec: specRec{Commit: string(j.Spec.Commit), Environment: string(j.Spec.Environment), Settings: settings},
		State: string(j.State), Key: string(j.Key), Environment: fingerprintPtrToRec(j.Environment),
		SubmittedAt: j.SubmittedAt, StartedAt: j.StartedAt, FinishedAt: j.FinishedAt, CancelReason: j.CancelReason, ExitCode: j.ExitCode,
		ResultJob: string(j.ResultJob), CheckJob: string(j.CheckJob),
	}
	for _, n := range j.Notes {
		r.Notes = append(r.Notes, noteRec{Text: n.Text, At: n.At, Agent: n.Agent})
	}
	for _, p := range j.Plans {
		r.Plans = append(r.Plans, uint32(p))
	}
	return r, nil
}

func jobFromRec(r jobRec) (core.Job, error) {
	settings, err := settingsFromRec(r.Spec.Settings)
	if err != nil {
		return core.Job{}, fmt.Errorf("job %s: %w", r.ID, err)
	}
	j := core.Job{
		ID: core.JobID(r.ID), Spec: core.JobSpec{Commit: core.Commit(r.Spec.Commit), Environment: core.EnvironmentKind(r.Spec.Environment), Settings: settings},
		State: core.JobState(r.State), Key: core.JobKey(r.Key), Environment: fingerprintPtrFromRec(r.Environment),
		SubmittedAt: r.SubmittedAt, StartedAt: r.StartedAt, FinishedAt: r.FinishedAt, CancelReason: r.CancelReason, ExitCode: r.ExitCode,
		ResultJob: core.JobID(r.ResultJob), CheckJob: core.JobID(r.CheckJob),
	}
	for _, n := range r.Notes {
		j.Notes = append(j.Notes, core.Note{Text: n.Text, At: n.At, Agent: n.Agent})
	}
	for _, p := range r.Plans {
		j.Plans = append(j.Plans, core.PlanNumber(p))
	}
	return j, nil
}

func outcomeToRec(o core.Outcome) (outcomeRec, error) {
	switch o := o.(type) {
	case core.CheckOutcome:
		return outcomeRec{Kind: "check", Check: &checkOutcomeRec{DurationNS: int64(o.Duration)}}, nil
	case core.MeasureOutcome:
		body := &measureOutcomeRec{
			Metric: metricRec{Name: o.Metric.Name, Direction: string(o.Metric.Direction)}, Samples: o.Samples, CheckJob: string(o.CheckJob),
		}
		for _, ob := range o.Observations {
			body.Observations = append(body.Observations, observationRec{Name: ob.Name, Values: ob.Values})
		}
		return outcomeRec{Kind: "measure", Measure: body}, nil
	}
	return outcomeRec{}, fmt.Errorf("an outcome of type %T has no encoding", o)
}

func outcomeFromRec(r outcomeRec) (core.Outcome, error) {
	switch {
	case r.Kind == "check" && r.Check != nil && r.Measure == nil:
		return core.CheckOutcome{Duration: time.Duration(r.Check.DurationNS)}, nil
	case r.Kind == "measure" && r.Measure != nil && r.Check == nil:
		o := core.MeasureOutcome{
			Metric:  core.Metric{Name: r.Measure.Metric.Name, Direction: core.Direction(r.Measure.Metric.Direction)},
			Samples: r.Measure.Samples, CheckJob: core.JobID(r.Measure.CheckJob),
		}
		for _, ob := range r.Measure.Observations {
			o.Observations = append(o.Observations, core.Observation{Name: ob.Name, Values: ob.Values})
		}
		return o, nil
	}
	return nil, fmt.Errorf("an outcome of kind %q with the wrong bodies", r.Kind)
}

func resultToRec(r core.Result) (resultRec, error) {
	outcome, err := outcomeToRec(r.Outcome)
	if err != nil {
		return resultRec{}, fmt.Errorf("result of job %s: %w", r.Job, err)
	}
	return resultRec{
		Job: string(r.Job), Key: string(r.Key), Task: r.Task, TaskVersion: r.TaskVersion, Commit: string(r.Commit), SchemaVersion: r.SchemaVersion,
		Environment: fingerprintToRec(r.Environment),
		Machine: machineRec{
			HostID: r.Machine.HostID, OS: r.Machine.OS, Arch: r.Machine.Arch, CPUModel: r.Machine.CPUModel,
			LogicalCPUs: r.Machine.LogicalCPUs, MemoryBytes: r.Machine.MemoryBytes,
		},
		Load: loadRec{
			SampledAt: r.Load.SampledAt, LoadAverage1m: r.Load.LoadAverage1m, CPUBusyFraction: r.Load.CPUBusyFraction,
			MemoryAvailableBytes: r.Load.MemoryAvailableBytes,
		},
		RecordedAt: r.RecordedAt, Outcome: outcome,
	}, nil
}

func resultFromRec(r resultRec) (core.Result, error) {
	outcome, err := outcomeFromRec(r.Outcome)
	if err != nil {
		return core.Result{}, fmt.Errorf("result of job %s: %w", r.Job, err)
	}
	return core.Result{
		Job: core.JobID(r.Job), Key: core.JobKey(r.Key), Task: r.Task, TaskVersion: r.TaskVersion, Commit: core.Commit(r.Commit), SchemaVersion: r.SchemaVersion,
		Environment: fingerprintFromRec(r.Environment),
		Machine: core.MachineFacts{
			HostID: r.Machine.HostID, OS: r.Machine.OS, Arch: r.Machine.Arch, CPUModel: r.Machine.CPUModel,
			LogicalCPUs: r.Machine.LogicalCPUs, MemoryBytes: r.Machine.MemoryBytes,
		},
		Load: core.MachineLoad{
			SampledAt: r.Load.SampledAt, LoadAverage1m: r.Load.LoadAverage1m, CPUBusyFraction: r.Load.CPUBusyFraction,
			MemoryAvailableBytes: r.Load.MemoryAvailableBytes,
		},
		RecordedAt: r.RecordedAt, Outcome: outcome,
	}, nil
}

func verdictToRec(v core.Verdict) verdictRec {
	return verdictRec{
		Kind: string(v.Kind), Reason: string(v.Reason), CandidateSamples: v.CandidateSamples, BaseSamples: v.BaseSamples,
		CandidateMean: v.CandidateMean, BaseMean: v.BaseMean, Ratio: v.Ratio, Noise: v.Noise,
	}
}

func verdictFromRec(r verdictRec) core.Verdict {
	return core.Verdict{
		Kind: core.VerdictKind(r.Kind), Reason: core.InconclusiveReason(r.Reason), CandidateSamples: r.CandidateSamples, BaseSamples: r.BaseSamples,
		CandidateMean: r.CandidateMean, BaseMean: r.BaseMean, Ratio: r.Ratio, Noise: r.Noise,
	}
}

func promotionToRec(p core.Promotion) promotionRec {
	return promotionRec{
		Commit: string(p.Commit), PreviousHead: string(p.PreviousHead), Verdict: verdictToRec(p.Verdict),
		Environment: fingerprintToRec(p.Environment), ResultJob: string(p.ResultJob), At: p.At,
	}
}

func promotionFromRec(r promotionRec) core.Promotion {
	return core.Promotion{
		Commit: core.Commit(r.Commit), PreviousHead: core.Commit(r.PreviousHead), Verdict: verdictFromRec(r.Verdict),
		Environment: fingerprintFromRec(r.Environment), ResultJob: core.JobID(r.ResultJob), At: r.At,
	}
}

func planToRec(p core.Plan) planRec {
	r := planRec{
		Number: uint32(p.Number), Title: p.Title, Body: p.Body, Status: string(p.Status), CreatedAt: p.CreatedAt, UpdatedAt: p.UpdatedAt,
	}
	for _, c := range p.Comments {
		r.Comments = append(r.Comments, commentRec{Text: c.Text, At: c.At, Agent: c.Agent})
	}
	return r
}

func planFromRec(r planRec) core.Plan {
	p := core.Plan{
		Number: core.PlanNumber(r.Number), Title: r.Title, Body: r.Body, Status: core.PlanStatus(r.Status), CreatedAt: r.CreatedAt, UpdatedAt: r.UpdatedAt,
	}
	for _, c := range r.Comments {
		p.Comments = append(p.Comments, core.PlanComment{Text: c.Text, At: c.At, Agent: c.Agent})
	}
	return p
}

func detailToRec(d core.EventDetail) (*detailRec, error) {
	switch d := d.(type) {
	case nil:
		return nil, nil
	case core.Job:
		job, err := jobToRec(d)
		return &detailRec{Kind: "job", Job: &job}, err
	case core.Result:
		result, err := resultToRec(d)
		return &detailRec{Kind: "result", Result: &result}, err
	case core.Verdict:
		v := verdictToRec(d)
		return &detailRec{Kind: "verdict", Verdict: &v}, nil
	case core.Promotion:
		p := promotionToRec(d)
		return &detailRec{Kind: "promotion", Promotion: &p}, nil
	case core.PromoteRefusal:
		r := refusalRec{}
		for _, reason := range d.Reasons {
			rr := refusalReasonRec{
				Kind: string(reason.Kind), Message: reason.Message, MeasuredAgainst: string(reason.MeasuredAgainst), CurrentHead: string(reason.CurrentHead),
			}
			if reason.Verdict != nil {
				v := verdictToRec(*reason.Verdict)
				rr.Verdict = &v
			}
			r.Reasons = append(r.Reasons, rr)
		}
		return &detailRec{Kind: "refusal", Refusal: &r}, nil
	case core.Failure:
		return &detailRec{Kind: "failure", Failure: &failureRec{Kind: string(d.Kind), Message: d.Message}}, nil
	case core.Plan:
		p := planToRec(d)
		return &detailRec{Kind: "plan", Plan: &p}, nil
	}
	return nil, fmt.Errorf("an event detail of type %T has no encoding", d)
}

func detailFromRec(r *detailRec) (core.EventDetail, error) {
	if r == nil {
		return nil, nil
	}
	bodies := 0
	for _, present := range []bool{r.Job != nil, r.Result != nil, r.Verdict != nil, r.Promotion != nil, r.Refusal != nil, r.Failure != nil, r.Plan != nil} {
		if present {
			bodies++
		}
	}
	if bodies != 1 {
		return nil, fmt.Errorf("an event detail of kind %q with %d bodies", r.Kind, bodies)
	}
	switch {
	case r.Kind == "job" && r.Job != nil:
		return jobFromRec(*r.Job)
	case r.Kind == "result" && r.Result != nil:
		return resultFromRec(*r.Result)
	case r.Kind == "verdict" && r.Verdict != nil:
		return verdictFromRec(*r.Verdict), nil
	case r.Kind == "promotion" && r.Promotion != nil:
		return promotionFromRec(*r.Promotion), nil
	case r.Kind == "refusal" && r.Refusal != nil:
		out := core.PromoteRefusal{}
		for _, rr := range r.Refusal.Reasons {
			reason := core.PromoteRefusalReason{
				Kind: core.PromoteRefusalKind(rr.Kind), Message: rr.Message, MeasuredAgainst: core.Commit(rr.MeasuredAgainst), CurrentHead: core.Commit(rr.CurrentHead),
			}
			if rr.Verdict != nil {
				v := verdictFromRec(*rr.Verdict)
				reason.Verdict = &v
			}
			out.Reasons = append(out.Reasons, reason)
		}
		return out, nil
	case r.Kind == "failure" && r.Failure != nil:
		return core.Failure{Kind: core.FailureKind(r.Failure.Kind), Message: r.Failure.Message}, nil
	case r.Kind == "plan" && r.Plan != nil:
		return planFromRec(*r.Plan), nil
	}
	return nil, fmt.Errorf("an event detail of kind %q with the wrong body", r.Kind)
}

func eventToRec(e core.Event) (eventRec, error) {
	detail, err := detailToRec(e.Detail)
	if err != nil {
		return eventRec{}, fmt.Errorf("event %d of run %s: %w", e.Sequence, e.Run, err)
	}
	return eventRec{
		Sequence: e.Sequence, SchemaVersion: e.SchemaVersion, Run: string(e.Run), HostID: e.HostID, Task: e.Task, Commit: string(e.Commit),
		Plan: uint32(e.Plan), Harness: e.Harness, Agent: e.Agent, At: e.At, Command: string(e.Command), Outcome: string(e.Outcome),
		Job: string(e.Job), Detail: detail,
	}, nil
}

func eventFromRec(r eventRec) (core.Event, error) {
	detail, err := detailFromRec(r.Detail)
	if err != nil {
		return core.Event{}, fmt.Errorf("event %d of run %s: %w", r.Sequence, r.Run, err)
	}
	return core.Event{Sequence: r.Sequence, EventDraft: core.EventDraft{
		SchemaVersion: r.SchemaVersion, Run: core.RunID(r.Run), HostID: r.HostID, Task: r.Task, Commit: core.Commit(r.Commit),
		Plan: core.PlanNumber(r.Plan), Harness: r.Harness, Agent: r.Agent, At: r.At, Command: core.Command(r.Command), Outcome: core.CommandOutcome(r.Outcome),
		Job: core.JobID(r.Job), Detail: detail,
	}}, nil
}

// savedToFile encodes a result with its place in the order results were saved.
func savedToFile(s ledger.SavedResult) (resultFile, error) {
	r, err := resultToRec(s.Result)
	return resultFile{Format: formatVersion, Seq: s.Seq, Result: r}, err
}
