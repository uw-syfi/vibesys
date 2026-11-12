package core

import (
	"slices"
	"time"
)

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

// JobStage is the step of a check or measure job that ended it: the zero value is none.
type JobStage string

// The stages a failed check or measure job can end in.
const (
	// StageBuild is a candidate or task image that did not build.
	StageBuild JobStage = "build"
	// StageCheck is the correctness check, which ran and rejected the candidate or could not complete.
	StageCheck JobStage = "check"
	// StageMeasure is the benchmark that takes a measure job's samples.
	StageMeasure JobStage = "measure"
)

// Valid says whether the stage is one of the three, so the zero value (none) is not.
func (s JobStage) Valid() bool {
	return s == StageBuild || s == StageCheck || s == StageMeasure
}

// JobState is where a job is.
type JobState string

// The job states. Succeeded, Failed, Cancelled and Lost are final.
const (
	JobQueued    JobState = "queued"
	JobRunning   JobState = "running"
	JobSucceeded JobState = "succeeded"
	JobFailed    JobState = "failed"
	JobCancelled JobState = "cancelled"
	// JobLost means the machine or the supervisor went away while the job ran, and the job is not
	// run again: its command may have started and RerunPolicyOf its kind does not rerun it after
	// that, or it was cut off as often as MaxAttempts allows.
	JobLost JobState = "lost"
)

// JobStates lists every state, queued first.
func JobStates() []JobState {
	return []JobState{JobQueued, JobRunning, JobSucceeded, JobFailed, JobCancelled, JobLost}
}

// Final reports whether a job in this state can no longer change state: a known state with no
// legal move out of it (see CanMoveTo).
func (s JobState) Final() bool {
	return slices.Contains(JobStates(), s) && len(s.next()) == 0
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

// CommandImage is the image a command job runs in, which a submission always names.
type CommandImage string

const (
	// ImageUnspecified is a job stored before the image was named, whose Candidate says where it
	// ran (CommandSettings.RanIn). A submission that names none is refused.
	ImageUnspecified CommandImage = ""
	// ImageHarness is the task's image, built from the task's files, where its check and benchmark
	// run (the host for a task with no Dockerfile). On a task of two images the command runs there
	// beside the candidate's server, as the check and the benchmark do.
	ImageHarness CommandImage = "harness"
	// ImageCandidate is the candidate's image, built from the job's commit by the task's
	// [candidate] dockerfile. Only a task of two images has one.
	ImageCandidate CommandImage = "candidate"
)

// CommandImages is every image a submission may name, in the order a message lists them.
func CommandImages() []CommandImage { return []CommandImage{ImageHarness, ImageCandidate} }

// CommandSettings configures a command job.
type CommandSettings struct {
	Argv    []string
	Service *ServiceSpec
	// Timeout is zero for the default limit.
	Timeout time.Duration
	// Image is the image the command runs in, which the submission names.
	Image CommandImage
	// Wrap is an argv prefix the daemon applies when it accepts the job: in front of the
	// candidate's server for a command in the task's image of a task of two images, and otherwise
	// in front of the service when there is one and of the command when not. A stored job holds
	// it applied, so it is empty there.
	Wrap []string
	// Candidate is set by the daemon for a job of a task of two images: how the job's candidate image
	// is built, and the server a command in the task's image runs against. It is never the caller's.
	Candidate *CommandCandidate
}

// RanIn is the image the command runs in: the one it names, or for a job stored before an image was
// named, the one its candidate setup implies.
func (c CommandSettings) RanIn() CommandImage {
	switch {
	case c.Image != ImageUnspecified:
		return c.Image
	case c.Candidate != nil && c.Candidate.Serve == nil:
		return ImageCandidate
	}
	return ImageHarness
}

// CommandCandidate is how a command job of a task of two images uses the candidate's image.
type CommandCandidate struct {
	Build CandidateBuild
	// Serve, set for a command in the task's image, starts the server in the candidate's image, with
	// the checkout's root not yet resolved, and the command runs in the task's image against it. Nil
	// runs the command in the candidate's image.
	Serve *CandidateServe
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
	// Placement restricts a docker job to one placement of the operator config. Empty lets it run
	// on any placement that gives what its task needs (EligiblePlacements), which the scheduler
	// chooses when the job starts; the placement it ran on is in its pinned environment
	// (Fingerprint.Placement). A host job has none.
	Placement string
	// Allocation is the standing allocation of the submitting run the job runs in: an allocation's
	// name (alloc.ID), which refuses the job unless it fits that one, or AllocationAny, which puts it
	// in a running allocation of the run that it fits and otherwise queues it as a job with no
	// allocation is queued. Empty runs it as it always has.
	Allocation string
	Settings   Settings
}

// AllocationAny is JobSpec.Allocation for a job that takes any allocation of its run it fits.
const AllocationAny = "any"

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

// Submitter is one caller that submitted a job: the submission that created it, or one that joined it
// or was answered from it. Run and Agent are what the submission's call context named, and either
// is empty when the caller named none (a person at a terminal).
type Submitter struct {
	Run   RunID
	Agent string
	// At is when this caller first submitted the job.
	At time.Time
}

// Job is a submission and where it stands.
type Job struct {
	ID    JobID
	Spec  JobSpec
	State JobState
	// Key is empty for command jobs. A job that starts in an environment other than the one it was
	// keyed in, such as a placement whose engine built the task's image to another digest, runs
	// under the key of the environment it starts in (Transition.Key).
	Key JobKey
	// Needs are the resources the task at the job's commit needs of the placement a docker job runs
	// on, decided when it was submitted. Only a placement that gives them all runs it.
	Needs []ResourceKind
	// TaskVersion is the version of the task a check or measure job was submitted for, which it
	// runs only if the task is still at it when the job starts. It is empty for a command job, and
	// for a job of a store of version 4 or older, which runs the task as it is then.
	TaskVersion string
	// FailedStage is the stage that ended a failed check or measure job, empty for a job that did not
	// fail, a command job, a failure outside every stage, and a job saved before it was recorded.
	FailedStage JobStage
	// NoCache is the submission's choice to run the whole job fresh: it neither joined nor reused
	// a job, and when it starts it reuses no saved samples and no saved check pass. It is kept on
	// the job because the job may start long after, in another daemon.
	NoCache bool
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
	// Attempts are the earlier attempts at the job that were cut off, oldest first: each ended
	// when its supervisor was proved gone, and the job was queued again. The current attempt is
	// number len(Attempts)+1, and there are always fewer than MaxAttempts.
	Attempts []PastAttempt
	// Submitters are the callers that submitted the job, one for each distinct (Run, Agent), oldest
	// first. A submission that joins or reuses the job adds its caller, also once the job is final.
	Submitters []Submitter
}

// MaxAttempts is the most attempts a job gets. A job whose supervisor is proved gone is queued again
// only while it has had fewer, so a supervisor that dies every time cannot run a job for ever. It
// counts attempts cut off before the command started too.
const MaxAttempts = 3

// PastAttempt is an attempt at a job that was cut off, after which the job was queued again.
type PastAttempt struct {
	// StartedAt is when the store recorded the attempt running, and EndedAt when it was found cut
	// off.
	StartedAt, EndedAt time.Time
	Cut                Cut
}

// Cut is how an attempt was cut off: before its command was told to start, or after it may have
// started. The set is closed.
type Cut interface {
	// Cause is how the attempt's supervisor was proved gone.
	Cause() CutCause
	cut()
}

// CutBeforeStart is an attempt whose supervisor was proved gone before its command was told to
// start, so nothing of it ran.
type CutBeforeStart struct{ By CutCause }

// CutMidCommand is an attempt whose supervisor was proved gone after its command may have started.
type CutMidCommand struct{ By CutCause }

// Cause implements Cut.
func (c CutBeforeStart) Cause() CutCause { return c.By }

// Cause implements Cut.
func (c CutMidCommand) Cause() CutCause { return c.By }

func (CutBeforeStart) cut() {}
func (CutMidCommand) cut()  {}

// CutCause is how a supervisor was proved gone. The set is closed.
type CutCause string

// The causes.
const (
	// CutSupervisorDied is a supervisor whose lock could be taken.
	CutSupervisorDied CutCause = "supervisor died"
	// CutRebooted is a machine that started again since the attempt began.
	CutRebooted CutCause = "machine restarted"
)

// Valid says the cause is one of the causes.
func (c CutCause) Valid() bool { return c == CutSupervisorDied || c == CutRebooted }

// RerunPolicy is when a job whose supervisor is proved gone may run again. The set is closed.
type RerunPolicy string

// The policies.
const (
	// RerunBeforeStartOnly runs a job again only when its command never started. A command job may
	// have done anything once it started, so it is not run twice.
	RerunBeforeStartOnly RerunPolicy = "before start only"
	// RerunAfterProvenDeath runs a job again whenever its supervisor is proved gone. A check or a
	// measure job saves nothing until it passes, so a second attempt cannot meet half of the first.
	RerunAfterProvenDeath RerunPolicy = "after proven death"
)

// RerunPolicyOf is the rerun policy of a kind of job.
func RerunPolicyOf(kind JobKind) RerunPolicy {
	switch kind {
	case JobCheck, JobMeasure:
		return RerunAfterProvenDeath
	case JobCommand:
	}
	return RerunBeforeStartOnly
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
	Key JobKey
	// Needs are the resources the job's task needs of a placement (Job.Needs).
	Needs []ResourceKind
	// TaskVersion is the version of the task a check or measure job is for (Job.TaskVersion).
	TaskVersion string
	NoCache     bool
	// Note is attached to the job a submission lands on, including a joined or reused one.
	Note *Note
	Plan PlanNumber
	At   time.Time
}

// SubmitterOf is the caller a submission names: its run and agent, at the time it was made.
func SubmitterOf(sub Submission) Submitter {
	return Submitter{Run: sub.Context.Run, Agent: sub.Context.Agent, At: sub.At}
}

// WithSubmitter returns the submitters with s added, and says whether it was: a caller that is
// already there (the same run and the same agent) is not added again, and keeps its first time.
func WithSubmitter(submitters []Submitter, s Submitter) ([]Submitter, bool) {
	for _, have := range submitters {
		if have.Run == s.Run && have.Agent == s.Agent {
			return submitters, false
		}
	}
	return append(slices.Clone(submitters), s), true
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
	// Key, set only on the move to running of a check or measure job, is the key the job runs
	// under from then on: that of the environment it pinned. Empty keeps the key it was queued
	// under.
	Key JobKey
	// Result is saved with a move to succeeded and is nil otherwise.
	Result       *Result
	ExitCode     *int
	CancelReason string
	// CheckJob links a measure job to the check pass it relies on.
	CheckJob JobID
	// FailedStage is the stage that ended a check or measure job, set on the move to failed and only
	// there; empty when no stage of the job's record ended it.
	FailedStage JobStage
	// Cut is set on the move from running back to queued, and only there: it says how the attempt
	// that ran was cut off, and the job keeps it in Attempts.
	Cut Cut
}
