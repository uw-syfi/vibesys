package core

import "time"

// JobKind is what a job does.
type JobKind string

// The job kinds.
const (
	// JobCheck builds and runs the task's correctness check.
	JobCheck JobKind = "check"
	// JobCommand is an agent-defined cheap job. It is never cached or deduplicated.
	JobCommand JobKind = "command"
	// JobMeasure is the official measurement: build, check, benchmark samples, saved result.
	JobMeasure JobKind = "measure"
)

// JobState is where a job is.
type JobState string

// The job states. Succeeded, Failed, Cancelled and Lost are final.
const (
	JobQueued    JobState = "queued"
	JobRunning   JobState = "running"
	JobSucceeded JobState = "succeeded"
	JobFailed    JobState = "failed"
	JobCancelled JobState = "cancelled"
	// JobLost means the machine or the supervisor went away while the job ran. It is never retried.
	JobLost JobState = "lost"
)

// JobStates lists every state, queued first.
func JobStates() []JobState {
	return []JobState{JobQueued, JobRunning, JobSucceeded, JobFailed, JobCancelled, JobLost}
}

// Final reports whether a job in this state can no longer change state.
func (s JobState) Final() bool {
	switch s {
	case JobSucceeded, JobFailed, JobCancelled, JobLost:
		return true
	case JobQueued, JobRunning:
		return false
	}
	return false
}

// Settings is what a kind of job is configured with: exactly one of CheckSettings,
// CommandSettings and MeasureSettings. The interface is closed, so a JobSpec cannot carry a kind
// that disagrees with its settings.
type Settings interface {
	// Kind is the job kind these settings belong to.
	Kind() JobKind
	settings()
}

// CheckSettings configures a check job. It has none yet.
type CheckSettings struct{}

// ServiceSpec is a long-lived process a command job runs against. The environment owns its
// lifecycle and kills its whole process group when the job ends.
type ServiceSpec struct {
	Argv []string
	// Probe succeeds when the service is ready. Empty means ready as soon as it starts.
	Probe []string
}

// CommandSettings configures a command job.
type CommandSettings struct {
	Argv    []string
	Service *ServiceSpec
	// Timeout is zero for the default limit.
	Timeout time.Duration
}

// MeasureSettings configures a measure job.
type MeasureSettings struct {
	// Samples is how many benchmark samples are needed. Zero means the task's default.
	Samples uint32
	// Recheck runs a fresh check instead of reusing a matching pass.
	Recheck bool
}

// Kind implements Settings.
func (CheckSettings) Kind() JobKind { return JobCheck }

// Kind implements Settings.
func (CommandSettings) Kind() JobKind { return JobCommand }

// Kind implements Settings.
func (MeasureSettings) Kind() JobKind { return JobMeasure }

func (CheckSettings) settings()   {}
func (CommandSettings) settings() {}
func (MeasureSettings) settings() {}

// JobSpec is what a submission asks for.
type JobSpec struct {
	Commit Commit
	// Environment is the one the caller asks for. EnvironmentAuto lets the task choose.
	Environment EnvironmentKind
	Settings    Settings
}

// KeyInputs is everything a cache key is made of. Notes, plans and the caller never are. The
// digest over these is a pure rule that lives in this package.
type KeyInputs struct {
	Kind          JobKind
	Commit        Commit
	TaskVersion   string
	Environment   Fingerprint
	Settings      Settings
	SchemaVersion uint32
}

// Note is free text on a job. It never takes part in a cache key, a verdict or a promotion.
type Note struct {
	Text  string
	At    time.Time
	Agent string
}

// Job is a submission and where it stands.
type Job struct {
	ID    JobID
	Spec  JobSpec
	State JobState
	// Key is empty for command jobs.
	Key JobKey
	// Environment is pinned when the job starts and nil while it is queued.
	Environment *Fingerprint
	Notes       []Note
	// Plans are every plan a submission linked this job to, in link order.
	Plans       []PlanNumber
	SubmittedAt time.Time
	StartedAt   *time.Time
	FinishedAt  *time.Time
	// CancelReason is set when State is JobCancelled.
	CancelReason string
	ExitCode     *int
	// ResultJob is the job whose saved result this job produced or reused.
	ResultJob JobID
	// CheckJob is the check pass a measure job links to.
	CheckJob JobID
}

// JobFilter selects jobs. Fields combine with AND and the zero value matches every job.
type JobFilter struct {
	Commit       Commit
	States       []JobState
	Kind         JobKind
	NoteContains string
}

// Submission is a request to queue a job.
type Submission struct {
	Context CallContext
	Spec    JobSpec
	// Key is the job's cache key, empty for a command job.
	Key     JobKey
	NoCache bool
	// Note is attached to the job a submission lands on, including a joined or reused one.
	Note *Note
	Plan PlanNumber
	At   time.Time
}

// SubmitDisposition says what a submission turned into.
type SubmitDisposition string

// The dispositions.
const (
	// SubmitCreated queued a new job.
	SubmitCreated SubmitDisposition = "created"
	// SubmitJoined landed on a queued or running job with the same key.
	SubmitJoined SubmitDisposition = "joined"
	// SubmitReused landed on the finished job whose saved result answers the submission.
	SubmitReused SubmitDisposition = "reused"
)

// SubmitOutcome is the job a submission landed on and how.
type SubmitOutcome struct {
	Job         Job
	Disposition SubmitDisposition
}

// Transition moves a job to a new state. The store applies it only if the job is still in From,
// and saves Result and the job's new state as one change, so a result exists only for a job that
// succeeded.
type Transition struct {
	Job  JobID
	From JobState
	To   JobState
	At   time.Time
	// Environment is set on the move to running.
	Environment *Fingerprint
	// Result is saved with a move to succeeded and is nil otherwise.
	Result       *Result
	ExitCode     *int
	CancelReason string
	// CheckJob links a measure job to the check pass it relies on.
	CheckJob JobID
}
