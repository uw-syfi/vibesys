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
	// LaunchTimeout is how long a session has to open before it is written off as failed to start.
	// It is longer than every bound inside a launch added together: installing the plugin (two
	// commands of 60 s each), the agent's own start (60 s), waiting for its tools (30 s) and starting
	// the process that holds it (30 s) come to four minutes, so a launch that is only slow ends with
	// its own typed reason and this timer is the backstop for one that ends with none.
	LaunchTimeout = 5 * time.Minute
	// MaxLeadResumes is how many sessions in a row the lead may lose before its run fails.
	MaxLeadResumes = 3
	// LeadIdleAfter is how long a run may stand still before its lead is reminded.
	LeadIdleAfter = 10 * time.Minute
	// MaxLeadReminders is how many reminders a run's lead gets in all. A lead that does not act on
	// three is confused, and each further reminder would only cost money; the run ends, for having
	// stalled, once LeadIdleAfter passes after the last.
	MaxLeadReminders = 3
	// LimitGrace is how long a lead has to finish after its run reached a limit. When it has passed,
	// or when the lead has given its final report, the run stops.
	LimitGrace = 10 * time.Minute
	// DefaultSummaryEvery is how often a run's lead is asked for a progress report while the run's work
	// moves, when the start did not say. The least an operator may ask for is five minutes, which the
	// contract's validation holds; the core takes the value it is given.
	DefaultSummaryEvery = time.Hour
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

// RunLimit is a limit an operator can put on a run. The empty value is "none".
type RunLimit string

// The limits.
const (
	// LimitTime is how long the run may take: its deadline.
	LimitTime RunLimit = "time"
	// LimitCost is what the run may cost, as the finished turns of its agents report it.
	LimitCost RunLimit = "cost"
)

// stopReason is why a run stops for having reached the limit.
func (l RunLimit) stopReason() RunStopReason {
	if l == LimitCost {
		return StopCostLimit
	}
	return StopTimeLimit
}

// RunStopReason says why a run left RunRunning.
type RunStopReason string

// The stop reasons. The empty reason means the run has not been stopped.
const (
	StopOperator  RunStopReason = "operator"
	StopTimeLimit RunStopReason = "time_limit"
	StopCostLimit RunStopReason = "cost_limit"
	StopLeadGone  RunStopReason = "lead_gone"
	// StopFinished is a run whose lead gave its final report and whose work then came to rest.
	StopFinished RunStopReason = "finished"
	// StopStalled is a run that stood still, its lead reminded the most times it is, and went on standing
	// still.
	StopStalled RunStopReason = "stalled"
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

// Working says the agent is doing, or about to do, the run's work: its session is opening, a turn is
// running, or it waits on subagents that are. It is the one definition of an agent that is working that
// every count a client shows is made with.
func (s AgentStatus) Working() bool {
	return s == AgentLaunching || s == AgentActive || s == AgentWaiting
}

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

// The triggers. TriggerSchedule is the wakeup of a timer: the lead's progress report coming due.
const (
	TriggerBrief           TurnTrigger = "brief"
	TriggerOperator        TurnTrigger = "operator"
	TriggerHandoff         TurnTrigger = "handoff"
	TriggerJobFinished     TurnTrigger = "job_finished"
	TriggerSchedule        TurnTrigger = "schedule"
	TriggerMessage         TurnTrigger = "message"
	TriggerSubagentSettled TurnTrigger = "subagent_settled"
	TriggerReminder        TurnTrigger = "reminder"
)

// Verdict is what a judge found of a candidate's claim.
type Verdict string

// The verdicts.
const (
	VerdictPass       Verdict = "pass"
	VerdictFail       Verdict = "fail"
	VerdictUnmeasured Verdict = "unmeasured"
)

// ReportScope says what a report is about. The core sets it when it records the report, from the turn
// the report is posted in: a lead never chooses it. The zero value is a report recorded before scope
// existed, which is about the whole run.
type ReportScope string

// The scopes.
const (
	// ReportScopeRun is a report about the whole run. The run's summary is the newest of these.
	ReportScopeRun ReportScope = "run"
	// ReportScopeWindow is a progress report that answers a check-in: it covers the check-in's window and
	// no more, so it is never the run's summary and does not count as the lead's scheduled report.
	ReportScopeWindow ReportScope = "window"
)

// ReportKind says what a report is for.
type ReportKind string

// The report kinds. A final report is the lead's last word on what it was asked to do.
const (
	ReportProgress ReportKind = "progress"
	ReportFinal    ReportKind = "final"
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
	RefuseRunNotFound   Refusal = "run_not_found"
	RefuseAgentNotFound Refusal = "agent_not_found"
	RefuseNotPermitted  Refusal = "not_permitted"
	RefuseRunNotRunning Refusal = "run_not_running"
	RefuseFanoutLimit   Refusal = "fanout_limit"
	RefuseAgentIsLead   Refusal = "agent_is_lead"
	RefuseAgentNotLive  Refusal = "agent_not_live"
	// RefuseRepositoryInUse says another run that is not over works in the repository. The
	// receipt's run is that run.
	RefuseRepositoryInUse Refusal = "repository_in_use"
	// RefuseAgentNotAllowed says the harness and model asked for are outside what the run may use.
	// The receipt lists what is allowed.
	RefuseAgentNotAllowed Refusal = "agent_not_allowed"
	// RefuseTimeLimitReached and RefuseCostLimitReached say the run reached that limit and starts no new
	// agent.
	RefuseTimeLimitReached Refusal = "time_limit_reached"
	RefuseCostLimitReached Refusal = "cost_limit_reached"
	// RefuseNoViewer says a catch-up was asked for by a caller with no viewer.
	RefuseNoViewer Refusal = "no_viewer"
	// RefuseModelNotAllowed says the host's policy forbids the model of an agent the command would
	// launch. The receipt names the model and lists the models the host allows.
	RefuseModelNotAllowed Refusal = "model_not_allowed"
	// RefuseHandoffNotYours says a handoff has no body, or the body of another role than its poster's.
	RefuseHandoffNotYours Refusal = "handoff_not_yours"
	// RefuseEvalPlaceNotFound, RefuseEvalPlaceMisfit and RefuseEvalPlaceAmbiguous say the eval place
	// the start named is not one the host has, does not give the task's accelerator, or was not named
	// and several give it. The receipt lists the places.
	RefuseEvalPlaceNotFound  Refusal = "eval_place_not_found"
	RefuseEvalPlaceMisfit    Refusal = "eval_place_misfit"
	RefuseEvalPlaceAmbiguous Refusal = "eval_place_ambiguous"
	// RefuseSetupIncomplete says a setup was completed before every check passed. The receipt lists
	// the checks that have not.
	RefuseSetupIncomplete Refusal = "setup_incomplete"
	// RefuseNotInSetup says a setup-only command named a run that is not in setup.
	RefuseNotInSetup Refusal = "not_in_setup"
)

// StartFailure says why a session never opened. The empty value means it did not fail to start.
type StartFailure string

// The start failures.
const (
	StartAgentNotInstalled     StartFailure = "agent_not_installed"
	StartNotSignedIn           StartFailure = "not_signed_in"
	StartPluginUnavailable     StartFailure = "plugin_unavailable"
	StartToolsUnavailable      StartFailure = "tools_unavailable"
	StartWorkingDirUnavailable StartFailure = "working_directory_unavailable"
	StartTimedOut              StartFailure = "timed_out"
	StartOther                 StartFailure = "other"
	// StartIncompatibleVersions says what holds the session and this host share no protocol.
	StartIncompatibleVersions StartFailure = "incompatible_versions"
	// StartStateLocationTooLong says the place the host keeps sessions in is too long to reach one.
	StartStateLocationTooLong StartFailure = "state_location_too_long"
	// StartModelNotAllowed says the host's model policy forbids the model the session was to run on.
	StartModelNotAllowed StartFailure = "model_not_allowed"
)

// TimerKind says what a timer is for.
type TimerKind string

// The timer kinds.
const (
	TimerRunDeadline TimerKind = "run_deadline"
	TimerStopGrace   TimerKind = "stop_grace"
	// TimerLaunchDeadline ends a session that has not opened within LaunchTimeout.
	TimerLaunchDeadline TimerKind = "launch_deadline"
	TimerLeadIdle       TimerKind = "lead_idle"
	// TimerLimitGrace ends the wait for a lead that was told its run reached a limit.
	TimerLimitGrace TimerKind = "limit_grace"
	// TimerSummaryDue is when a lead that work has moved under is next asked for a progress report.
	TimerSummaryDue TimerKind = "summary_due"
)
