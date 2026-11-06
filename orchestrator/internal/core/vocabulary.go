package core

import "time"

// The bounds of the decisions. They are constants because no run needs another value; a test that
// wants to reach one reaches it in simulated time or in a few steps.
const (
	// StopGrace is how long a session has to confirm a stop before it is written off as lost.
	StopGrace = 2 * time.Minute
	// MaxDeliveryAttempts is how many turns may be started for one message. A turn that is
	// interrupted or fails leaves its message undelivered, and a message that keeps killing turns
	// must not do so forever.
	MaxDeliveryAttempts = 3
	// MaxLeadResumes is how many sessions in a row the lead may lose before its run fails.
	MaxLeadResumes = 3
)

// The harnesses. The empty harness means "the lead's", on a spawn only.
const (
	ClaudeCode Harness = "claude_code"
	Codex      Harness = "codex"
)

// RunStatus is where a run is.
type RunStatus string

// The run statuses. RunEnded and RunFailed are final.
const (
	RunStarting RunStatus = "starting"
	RunRunning  RunStatus = "running"
	RunStopping RunStatus = "stopping"
	RunEnded    RunStatus = "ended"
	RunFailed   RunStatus = "failed"
)

// Live says the run can still take new work.
func (s RunStatus) Live() bool { return s == RunStarting || s == RunRunning }

// Final says the run is over.
func (s RunStatus) Final() bool { return s == RunEnded || s == RunFailed }

// RunStopReason says why a run left RunRunning.
type RunStopReason string

// The stop reasons. The empty reason means the run has not been stopped.
const (
	StopOperator RunStopReason = "operator"
	StopDeadline RunStopReason = "deadline"
	StopLeadGone RunStopReason = "lead_gone"
)

// AgentRole is what an agent is for.
type AgentRole string

// The roles.
const (
	RoleLead     AgentRole = "lead"
	RolePlanner  AgentRole = "planner"
	RoleWorker   AgentRole = "worker"
	RoleJudge    AgentRole = "judge"
	RoleCombiner AgentRole = "combiner"
	RoleProfiler AgentRole = "profiler"
)

// AgentStatus is where an agent's session is.
type AgentStatus string

// The agent statuses. AgentEnded, AgentFailed and AgentLost are final for the session.
const (
	AgentLaunching AgentStatus = "launching"
	AgentActive    AgentStatus = "active"
	AgentWaiting   AgentStatus = "waiting"
	AgentIdle      AgentStatus = "idle"
	AgentEnded     AgentStatus = "ended"
	AgentFailed    AgentStatus = "failed"
	AgentLost      AgentStatus = "lost"
)

// Over says the session is finished.
func (s AgentStatus) Over() bool { return s == AgentEnded || s == AgentFailed || s == AgentLost }

// Resting says the session is open and no turn is running.
func (s AgentStatus) Resting() bool { return s == AgentIdle || s == AgentWaiting }

// StopStage is how far an agent's stop has come.
type StopStage string

// The stop stages.
const (
	StopNone         StopStage = "none"
	StopInterrupting StopStage = "interrupting"
	StopClosing      StopStage = "closing"
)

// TurnTrigger is what started a turn.
type TurnTrigger string

// The triggers. TriggerSchedule is part of the contract and nothing starts such a turn yet.
const (
	TriggerBrief           TurnTrigger = "brief"
	TriggerOperator        TurnTrigger = "operator"
	TriggerHandoff         TurnTrigger = "handoff"
	TriggerJobFinished     TurnTrigger = "job_finished"
	TriggerSchedule        TurnTrigger = "schedule"
	TriggerMessage         TurnTrigger = "message"
	TriggerSubagentSettled TurnTrigger = "subagent_settled"
)

// TurnStatus is where a turn is.
type TurnStatus string

// The turn statuses. All but TurnRunning are final.
const (
	TurnRunning     TurnStatus = "running"
	TurnCompleted   TurnStatus = "completed"
	TurnInterrupted TurnStatus = "interrupted"
	TurnFailed      TurnStatus = "failed"
)

// ItemStatus is where an item is.
type ItemStatus string

// The item statuses. All but ItemRunning are final.
const (
	ItemRunning  ItemStatus = "running"
	ItemFinished ItemStatus = "finished"
	ItemFailed   ItemStatus = "failed"
)

// Delivery is where a message is.
type Delivery string

// The deliveries. DeliveryConsumed and DeliveryRejected are final.
const (
	DeliveryQueued   Delivery = "queued"
	DeliverySteered  Delivery = "steered"
	DeliveryNewTurn  Delivery = "new_turn"
	DeliveryConsumed Delivery = "consumed"
	DeliveryRejected Delivery = "rejected"
)

// Final says the message needs nothing more.
func (d Delivery) Final() bool { return d == DeliveryConsumed || d == DeliveryRejected }

// Rejection says why a message was not delivered.
type Rejection string

// The rejections. The empty rejection means the message was not rejected.
const (
	RejectAgentGone        Rejection = "agent_gone"
	RejectFanoutLimit      Rejection = "fanout_limit"
	RejectAgentStopping    Rejection = "agent_stopping"
	RejectRunStopping      Rejection = "run_stopping"
	RejectAttemptsExceeded Rejection = "attempts_exhausted"
)

// JobState is where a vibesys-cli job is.
type JobState string

// The job states. All but JobQueued and JobRunning are final.
const (
	JobQueued    JobState = "queued"
	JobRunning   JobState = "running"
	JobSucceeded JobState = "succeeded"
	JobFailed    JobState = "failed"
	JobCancelled JobState = "cancelled"
	JobLost      JobState = "lost"
)

// Final says the job is over.
func (s JobState) Final() bool { return s != JobQueued && s != JobRunning }

// Refusal says why a command was not carried out.
type Refusal string

// The refusals. The empty refusal means the command was accepted.
const (
	RefuseRunNotFound    Refusal = "run_not_found"
	RefuseAgentNotFound  Refusal = "agent_not_found"
	RefuseNotPermitted   Refusal = "not_permitted"
	RefuseRunNotRunning  Refusal = "run_not_running"
	RefuseFanoutLimit    Refusal = "fanout_limit"
	RefuseAgentIsLead    Refusal = "agent_is_lead"
	RefuseAgentNotLive   Refusal = "agent_not_live"
	RefuseDeadlinePassed Refusal = "deadline_passed"
	// RefuseRepositoryInUse says another run that is not over works in the repository. The
	// receipt's run is that run.
	RefuseRepositoryInUse Refusal = "repository_in_use"
)

// TimerKind says what a timer is for.
type TimerKind string

// The timer kinds.
const (
	TimerRunDeadline TimerKind = "run_deadline"
	TimerStopGrace   TimerKind = "stop_grace"
)
