package wire

import (
	"buf.build/go/protovalidate"
	"fmt"
	"maps"
	"slices"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
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

// Submitter returns the contract's message for a caller that submitted a job.
func Submitter(s core.Submitter) *cliv1.Submitter {
	return &cliv1.Submitter{RunId: string(s.Run), Agent: s.Agent, At: timestamppb.New(s.At)}
}

// ToSubmitter returns the domain submitter for the contract's message.
func ToSubmitter(s *cliv1.Submitter) core.Submitter {
	return core.Submitter{Run: core.RunID(s.GetRunId()), Agent: s.GetAgent(), At: toTime(s.GetAt())}
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
	out := &cliv1.JobSpec{Commit: string(s.Commit), Environment: EnvironmentKind(s.Environment), Placement: s.Placement, Allocation: s.Allocation}
	switch settings := s.Settings.(type) {
	case core.CheckSettings:
		out.Settings = &cliv1.JobSpec_Check{Check: &cliv1.CheckSettings{}}
	case core.CommandSettings:
		command := &cliv1.CommandSettings{
			Argv: append([]string(nil), settings.Argv...), Service: serviceSpec(settings.Service),
			Candidate: commandCandidate(settings.Candidate), Image: CommandImage(settings.Image), Wrap: slices.Clone(settings.Wrap),
		}
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
	if p := s.GetPlacement(); p != "" {
		if err := core.ValidPlacementName(p); err != nil {
			return core.JobSpec{}, invalid("%s.placement: %v", field, err)
		}
	}
	if a := s.GetAllocation(); a != "" && a != core.AllocationAny {
		if id := alloc.ID(a); !id.Valid() {
			return core.JobSpec{}, invalid("%s.allocation: %q is not an allocation (a name such as a7, or %q)", field, a, core.AllocationAny)
		}
	}
	out := core.JobSpec{Commit: commit, Environment: env, Placement: s.GetPlacement(), Allocation: s.GetAllocation()}
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
	if c.GetProfile() != nil {
		return core.CommandSettings{}, invalid("%s.profile: the profile job is retired; run the load as a command in the task's image, with the profiler wrapping the candidate's server: submit command <commit> --image harness --wrap '<profiler>' -- <load argv>", field)
	}
	candidate, err := ToCommandCandidate(field+".candidate", c.GetCandidate())
	if err != nil {
		return core.CommandSettings{}, err
	}
	image, err := ToCommandImage(field+".image", c.GetImage())
	if err != nil {
		return core.CommandSettings{}, err
	}
	if slices.Contains(c.GetWrap(), "") {
		return core.CommandSettings{}, invalid("%s.wrap: a word of the wrapper is empty", field)
	}
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
	return core.CommandSettings{
		Argv: append([]string(nil), c.GetArgv()...), Service: toServiceSpec(c.GetService()), Timeout: timeout,
		Candidate: candidate, Image: image, Wrap: slices.Clone(c.GetWrap()),
	}, nil
}

// Job returns the contract's message for a job.
func Job(j core.Job) *cliv1.Job {
	out := &cliv1.Job{
		Id:           string(j.ID),
		Spec:         JobSpec(j.Spec),
		State:        JobState(j.State),
		Key:          string(j.Key),
		NoCache:      j.NoCache,
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
	for _, a := range j.Attempts {
		out.Attempts = append(out.Attempts, Attempt(a))
	}
	for _, sub := range j.Submitters {
		out.Submitters = append(out.Submitters, Submitter(sub))
	}
	for _, k := range j.Needs {
		out.Needs = append(out.Needs, ResourceKind(k))
	}
	out.TaskVersion = j.TaskVersion
	out.FailedStage = JobStage(j.FailedStage)
	return out
}

// Attempt returns the contract's message for an attempt that was cut off.
func Attempt(a core.PastAttempt) *cliv1.Attempt {
	out := &cliv1.Attempt{StartedAt: timestamp(a.StartedAt), EndedAt: timestamp(a.EndedAt)}
	switch c := a.Cut.(type) {
	case core.CutBeforeStart:
		out.End = &cliv1.Attempt_CutBeforeStart{CutBeforeStart: CutCause(c.By)}
	case core.CutMidCommand:
		out.End = &cliv1.Attempt_CutMidCommand{CutMidCommand: CutCause(c.By)}
	}
	return out
}

// ToAttempt returns the domain attempt for the contract's message: it has an end, with a cause.
func ToAttempt(field string, a *cliv1.Attempt) (core.PastAttempt, error) {
	out := core.PastAttempt{StartedAt: toTime(a.GetStartedAt()), EndedAt: toTime(a.GetEndedAt())}
	switch end := a.GetEnd().(type) {
	case *cliv1.Attempt_CutBeforeStart:
		cause, err := ToCutCause(field+".cut_before_start", end.CutBeforeStart)
		if err != nil {
			return core.PastAttempt{}, err
		}
		out.Cut = core.CutBeforeStart{By: cause}
	case *cliv1.Attempt_CutMidCommand:
		cause, err := ToCutCause(field+".cut_mid_command", end.CutMidCommand)
		if err != nil {
			return core.PastAttempt{}, err
		}
		out.Cut = core.CutMidCommand{By: cause}
	default:
		return core.PastAttempt{}, invalid("%s: no end, so it was not cut off", field)
	}
	return out, nil
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
	if err := protovalidate.Validate(j); err != nil {
		return core.Job{}, fmt.Errorf("job: %w", err)
	}
	spec, err := ToJobSpec("job.spec", j.GetSpec())
	if err != nil {
		return core.Job{}, err
	}
	state, err := ToJobState("job.state", j.GetState())
	if err != nil {
		return core.Job{}, err
	}
	out := core.Job{
		ID: core.JobID(j.GetId()), Spec: spec, State: state, Key: core.JobKey(j.GetKey()), NoCache: j.GetNoCache(),
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
	if len(j.GetAttempts()) >= core.MaxAttempts {
		return core.Job{}, invalid("job.attempts: %d attempts cut off, and a job gets %d in all", len(j.GetAttempts()), core.MaxAttempts)
	}
	for i, a := range j.GetAttempts() {
		past, err := ToAttempt(fmt.Sprintf("job.attempts[%d]", i), a)
		if err != nil {
			return core.Job{}, err
		}
		out.Attempts = append(out.Attempts, past)
	}
	for _, sub := range j.GetSubmitters() {
		if err := sub.GetAt().CheckValid(); err != nil {
			return core.Job{}, fmt.Errorf("submitter.at: %w", err)
		}
		out.Submitters = append(out.Submitters, ToSubmitter(sub))
	}
	for i, k := range j.GetNeeds() {
		kind, err := ToResourceKind(fmt.Sprintf("job.needs[%d]", i), k)
		if err != nil {
			return core.Job{}, err
		}
		out.Needs = append(out.Needs, kind)
	}
	out.TaskVersion = j.GetTaskVersion()
	stage, err := ToJobStage("job.failed_stage", j.GetFailedStage())
	if err != nil {
		return core.Job{}, err
	}
	out.FailedStage = stage
	return out, nil
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
