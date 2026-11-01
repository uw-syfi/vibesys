package core

import "time"

// Command names a CLI command. Subcommands that differ in what they do are separate values.
type Command string

// The commands.
const (
	CommandSubmitCheck   Command = "submit_check"
	CommandSubmitCommand Command = "submit_command"
	CommandSubmitMeasure Command = "submit_measure"
	CommandWait          Command = "wait"
	CommandCancel        Command = "cancel"
	CommandNote          Command = "note"
	CommandJobs          Command = "jobs"
	CommandShow          Command = "show"
	CommandLogs          Command = "logs"
	CommandCompare       Command = "compare"
	CommandPromote       Command = "promote"
	CommandResult        Command = "result"
	CommandPlanCreate    Command = "plan_create"
	CommandPlanList      Command = "plan_list"
	CommandPlanView      Command = "plan_view"
	CommandPlanEdit      Command = "plan_edit"
	CommandPlanComment   Command = "plan_comment"
	CommandPlanClose     Command = "plan_close"
	CommandDaemon        Command = "daemon"
)

// CommandOutcome is how a command ended.
type CommandOutcome string

// The command outcomes.
const (
	OutcomeSucceeded CommandOutcome = "succeeded"
	// OutcomeFailed carries a Failure in the event's detail.
	OutcomeFailed CommandOutcome = "failed"
	// OutcomeRefused is a normal refusal, such as a refused promotion.
	OutcomeRefused CommandOutcome = "refused"
)

// EventDetail is what a command's event records about it: one of Job, Result, Verdict, Promotion,
// PromoteRefusal, Failure or Plan, or nil.
type EventDetail interface {
	eventDetail()
}

func (Job) eventDetail()            {}
func (Result) eventDetail()         {}
func (Verdict) eventDetail()        {}
func (Promotion) eventDetail()      {}
func (PromoteRefusal) eventDetail() {}
func (Failure) eventDetail()        {}
func (Plan) eventDetail()           {}

// EventDraft is an event before the store gives it a position.
type EventDraft struct {
	SchemaVersion uint32
	Run           RunID
	HostID        string
	Task          string
	Commit        Commit
	Plan          PlanNumber
	Harness       string
	Agent         string
	At            time.Time
	Command       Command
	Outcome       CommandOutcome
	Job           JobID
	Detail        EventDetail
}

// Event is one entry of the append-only record of what the commands did. Every command appends
// one, failures and refusals included.
type Event struct {
	// Sequence is the position in the run's log: it starts at 1 and has no gaps.
	Sequence uint64
	EventDraft
}

// LogChunk is a span of a job's output.
type LogChunk struct {
	Data []byte
	// Next is where the next read continues.
	Next uint64
}
