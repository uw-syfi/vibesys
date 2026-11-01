package wire

import (
	"maps"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func timestamp(t time.Time) *timestamppb.Timestamp {
	if t.IsZero() {
		return nil
	}
	return timestamppb.New(t)
}

func timePtr(t *time.Time) *timestamppb.Timestamp {
	if t == nil {
		return nil
	}
	return timestamp(*t)
}

func toTime(t *timestamppb.Timestamp) time.Time {
	if t == nil {
		return time.Time{}
	}
	return t.AsTime()
}

func toTimePtr(t *timestamppb.Timestamp) *time.Time {
	if t == nil {
		return nil
	}
	v := t.AsTime()
	return &v
}

// Fingerprint returns the contract's message for an environment fingerprint.
func Fingerprint(f core.Fingerprint) *cliv1.EnvironmentFingerprint {
	return &cliv1.EnvironmentFingerprint{
		Kind:        EnvironmentKind(f.Kind),
		Backend:     BackendKind(f.Backend),
		ImageDigest: f.ImageDigest,
		Facts:       maps.Clone(f.Facts),
	}
}

// ToFingerprint returns the domain fingerprint for the contract's message.
func ToFingerprint(field string, f *cliv1.EnvironmentFingerprint) (core.Fingerprint, error) {
	kind, err := ToEnvironmentKind(field+".kind", f.GetKind())
	if err != nil {
		return core.Fingerprint{}, err
	}
	backend, err := ToBackendKind(field+".backend", f.GetBackend())
	if err != nil {
		return core.Fingerprint{}, err
	}
	return core.Fingerprint{Kind: kind, Backend: backend, ImageDigest: f.GetImageDigest(), Facts: maps.Clone(f.GetFacts())}, nil
}

// Note returns the contract's message for a note.
func Note(n core.Note) *cliv1.Note {
	return &cliv1.Note{Text: n.Text, At: timestamp(n.At), Agent: n.Agent}
}

// ToNote returns the domain note for the contract's message.
func ToNote(n *cliv1.Note) core.Note {
	return core.Note{Text: n.GetText(), At: toTime(n.GetAt()), Agent: n.GetAgent()}
}

// ServiceSpec returns the contract's message for a service, or nil for none.
func serviceSpec(s *core.ServiceSpec) *cliv1.ServiceSpec {
	if s == nil {
		return nil
	}
	return &cliv1.ServiceSpec{Argv: append([]string(nil), s.Argv...), Probe: append([]string(nil), s.Probe...)}
}

func toServiceSpec(s *cliv1.ServiceSpec) *core.ServiceSpec {
	if s == nil {
		return nil
	}
	return &core.ServiceSpec{Argv: append([]string(nil), s.GetArgv()...), Probe: append([]string(nil), s.GetProbe()...)}
}

// JobSpec returns the contract's message for a job spec.
func JobSpec(s core.JobSpec) *cliv1.JobSpec {
	out := &cliv1.JobSpec{Commit: string(s.Commit), Environment: EnvironmentKind(s.Environment)}
	switch settings := s.Settings.(type) {
	case core.CheckSettings:
		out.Settings = &cliv1.JobSpec_Check{Check: &cliv1.CheckSettings{}}
	case core.CommandSettings:
		command := &cliv1.CommandSettings{Argv: append([]string(nil), settings.Argv...), Service: serviceSpec(settings.Service)}
		if settings.Timeout != 0 {
			command.Timeout = durationpb.New(settings.Timeout)
		}
		out.Settings = &cliv1.JobSpec_Command{Command: command}
	case core.MeasureSettings:
		out.Settings = &cliv1.JobSpec_Measure{Measure: &cliv1.MeasureSettings{Samples: settings.Samples, Recheck: settings.Recheck}}
	}
	return out
}

// ToJobSpec returns the domain spec for the contract's message, validated: the commit is a full
// lowercase hash, the settings are present, a command has an argument to run and no negative
// timeout, and the environment is one of the kinds.
func ToJobSpec(field string, s *cliv1.JobSpec) (core.JobSpec, error) {
	if s == nil {
		return core.JobSpec{}, invalid("%s: missing", field)
	}
	commit := core.Commit(s.GetCommit())
	if !commit.Valid() {
		return core.JobSpec{}, invalid("%s.commit: %q is not a full commit hash (40 lowercase hexadecimal characters)", field, s.GetCommit())
	}
	env, err := ToEnvironmentKind(field+".environment", s.GetEnvironment())
	if err != nil {
		return core.JobSpec{}, err
	}
	out := core.JobSpec{Commit: commit, Environment: env}
	switch settings := s.GetSettings().(type) {
	case *cliv1.JobSpec_Check:
		out.Settings = core.CheckSettings{}
	case *cliv1.JobSpec_Command:
		command, err := toCommandSettings(field+".command", settings.Command)
		if err != nil {
			return core.JobSpec{}, err
		}
		out.Settings = command
	case *cliv1.JobSpec_Measure:
		out.Settings = core.MeasureSettings{Samples: settings.Measure.GetSamples(), Recheck: settings.Measure.GetRecheck()}
	default:
		return core.JobSpec{}, invalid("%s: no job settings, so no job kind", field)
	}
	return out, nil
}

func toCommandSettings(field string, c *cliv1.CommandSettings) (core.CommandSettings, error) {
	if len(c.GetArgv()) == 0 || c.GetArgv()[0] == "" {
		return core.CommandSettings{}, invalid("%s.argv: a command needs a program to run", field)
	}
	if svc := c.GetService(); svc != nil && (len(svc.GetArgv()) == 0 || svc.GetArgv()[0] == "") {
		return core.CommandSettings{}, invalid("%s.service.argv: a service needs a program to run", field)
	}
	if p := c.GetService().GetProbe(); len(p) > 0 && p[0] == "" {
		return core.CommandSettings{}, invalid("%s.service.probe: a probe needs a program to run", field)
	}
	var timeout time.Duration
	if c.GetTimeout() != nil {
		if err := c.GetTimeout().CheckValid(); err != nil {
			return core.CommandSettings{}, invalid("%s.timeout: %v", field, err)
		}
		timeout = c.GetTimeout().AsDuration()
	}
	if timeout < 0 {
		return core.CommandSettings{}, invalid("%s.timeout: %v is negative", field, timeout)
	}
	return core.CommandSettings{Argv: append([]string(nil), c.GetArgv()...), Service: toServiceSpec(c.GetService()), Timeout: timeout}, nil
}

// Job returns the contract's message for a job.
func Job(j core.Job) *cliv1.Job {
	out := &cliv1.Job{
		Id:           string(j.ID),
		Spec:         JobSpec(j.Spec),
		State:        JobState(j.State),
		Key:          string(j.Key),
		SubmittedAt:  timestamp(j.SubmittedAt),
		StartedAt:    timePtr(j.StartedAt),
		FinishedAt:   timePtr(j.FinishedAt),
		CancelReason: j.CancelReason,
		ResultJobId:  string(j.ResultJob),
		CheckJobId:   string(j.CheckJob),
	}
	if j.Environment != nil {
		out.Environment = Fingerprint(*j.Environment)
	}
	for _, n := range j.Notes {
		out.Notes = append(out.Notes, Note(n))
	}
	for _, p := range j.Plans {
		out.Plans = append(out.Plans, uint32(p))
	}
	if j.ExitCode != nil {
		code := int32(*j.ExitCode)
		out.ExitCode = &code
	}
	return out
}

// Jobs returns the contract's messages for jobs, in order.
func Jobs(jobs []core.Job) []*cliv1.Job {
	out := make([]*cliv1.Job, 0, len(jobs))
	for _, j := range jobs {
		out = append(out, Job(j))
	}
	return out
}

// ToJob returns the domain job for the contract's message.
func ToJob(j *cliv1.Job) (core.Job, error) {
	spec, err := ToJobSpec("job.spec", j.GetSpec())
	if err != nil {
		return core.Job{}, err
	}
	state, err := ToJobState("job.state", j.GetState())
	if err != nil {
		return core.Job{}, err
	}
	out := core.Job{
		ID: core.JobID(j.GetId()), Spec: spec, State: state, Key: core.JobKey(j.GetKey()),
		SubmittedAt: toTime(j.GetSubmittedAt()), StartedAt: toTimePtr(j.GetStartedAt()), FinishedAt: toTimePtr(j.GetFinishedAt()),
		CancelReason: j.GetCancelReason(), ResultJob: core.JobID(j.GetResultJobId()), CheckJob: core.JobID(j.GetCheckJobId()),
	}
	if j.GetEnvironment() != nil {
		fp, err := ToFingerprint("job.environment", j.GetEnvironment())
		if err != nil {
			return core.Job{}, err
		}
		out.Environment = &fp
	}
	for _, n := range j.GetNotes() {
		out.Notes = append(out.Notes, ToNote(n))
	}
	for _, p := range j.GetPlans() {
		out.Plans = append(out.Plans, core.PlanNumber(p))
	}
	if j.ExitCode != nil {
		code := int(j.GetExitCode())
		out.ExitCode = &code
	}
	return out, nil
}

// ToCallContext returns the domain call context for the contract's message.
func ToCallContext(c *cliv1.CallContext) core.CallContext {
	return core.CallContext{
		Repository: c.GetRepository(), Task: c.GetTask(), Run: core.RunID(c.GetRunId()),
		Harness: c.GetHarness(), Agent: c.GetAgent(),
	}
}

// ToJobFilter returns the domain filter for a ListJobs request. A commit that is set must be a
// full hash, since a prefix would match nothing the store knows.
func ToJobFilter(r *cliv1.ListJobsRequest) (core.JobFilter, error) {
	kind, err := ToJobKind("kind", r.GetKind())
	if err != nil {
		return core.JobFilter{}, err
	}
	commit := core.Commit(r.GetCommit())
	if commit != "" && !commit.Valid() {
		return core.JobFilter{}, invalid("commit: %q is not a full commit hash (40 lowercase hexadecimal characters)", r.GetCommit())
	}
	filter := core.JobFilter{Commit: commit, Kind: kind, NoteContains: r.GetNoteContains()}
	for _, s := range r.GetStates() {
		state, err := ToJobState("states", s)
		if err != nil {
			return core.JobFilter{}, err
		}
		filter.States = append(filter.States, state)
	}
	return filter, nil
}

// LogChunk returns the contract's message for a span of a job's output.
func LogChunk(c core.LogChunk) *cliv1.ReadLogsResponse {
	return &cliv1.ReadLogsResponse{Data: c.Data, NextOffset: c.Next}
}
