package wire

import (
	"fmt"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// enum is a one-to-one table between a core closed set and a contract enum. The zero contract value
// stands for the core's empty value where a table lists it, and otherwise fails to parse.
type enum[C, P comparable] struct {
	toProto map[C]P
	toCore  map[P]C
}

func newEnum[C, P comparable](pairs map[C]P) enum[C, P] {
	e := enum[C, P]{toProto: pairs, toCore: make(map[P]C, len(pairs))}
	for c, p := range pairs {
		e.toCore[p] = c
	}
	return e
}

// out returns the contract value of a core value, which every core value has.
func (e enum[C, P]) out(c C) P { return e.toProto[c] }

// in returns the core value of a contract value, and notes one that has none.
func (e enum[C, P]) in(p P, errs *errorList) C {
	c, ok := e.toCore[p]
	if !ok {
		errs.addf("no core value for %v", p)
	}
	return c
}

var (
	runStatuses = newEnum(map[core.RunStatus]pb.RunStatus{
		core.RunStarting: pb.RunStatus_RUN_STATUS_STARTING,
		core.RunRunning:  pb.RunStatus_RUN_STATUS_RUNNING,
		core.RunStopping: pb.RunStatus_RUN_STATUS_STOPPING,
		core.RunEnded:    pb.RunStatus_RUN_STATUS_ENDED,
		core.RunFailed:   pb.RunStatus_RUN_STATUS_FAILED,
	})
	stopReasons = newEnum(map[core.RunStopReason]pb.RunStopReason{
		"":                pb.RunStopReason_RUN_STOP_REASON_UNSPECIFIED,
		core.StopOperator: pb.RunStopReason_RUN_STOP_REASON_OPERATOR,
		core.StopDeadline: pb.RunStopReason_RUN_STOP_REASON_DEADLINE,
		core.StopLeadGone: pb.RunStopReason_RUN_STOP_REASON_LEAD_GONE,
	})
	roles = newEnum(map[core.AgentRole]pb.AgentRole{
		core.RoleLead:     pb.AgentRole_AGENT_ROLE_LEAD,
		core.RolePlanner:  pb.AgentRole_AGENT_ROLE_PLANNER,
		core.RoleWorker:   pb.AgentRole_AGENT_ROLE_WORKER,
		core.RoleJudge:    pb.AgentRole_AGENT_ROLE_JUDGE,
		core.RoleCombiner: pb.AgentRole_AGENT_ROLE_COMBINER,
		core.RoleProfiler: pb.AgentRole_AGENT_ROLE_PROFILER,
	})
	agentStatuses = newEnum(map[core.AgentStatus]pb.AgentStatus{
		core.AgentLaunching: pb.AgentStatus_AGENT_STATUS_LAUNCHING,
		core.AgentActive:    pb.AgentStatus_AGENT_STATUS_ACTIVE,
		core.AgentWaiting:   pb.AgentStatus_AGENT_STATUS_WAITING,
		core.AgentIdle:      pb.AgentStatus_AGENT_STATUS_IDLE,
		core.AgentEnded:     pb.AgentStatus_AGENT_STATUS_ENDED,
		core.AgentFailed:    pb.AgentStatus_AGENT_STATUS_FAILED,
		core.AgentLost:      pb.AgentStatus_AGENT_STATUS_LOST,
	})
	stopStages = newEnum(map[core.StopStage]pb.AgentStopStage{
		core.StopNone:         pb.AgentStopStage_AGENT_STOP_STAGE_NONE,
		core.StopInterrupting: pb.AgentStopStage_AGENT_STOP_STAGE_INTERRUPTING,
		core.StopClosing:      pb.AgentStopStage_AGENT_STOP_STAGE_CLOSING,
	})
	triggers = newEnum(map[core.TurnTrigger]pb.TurnTrigger{
		core.TriggerBrief:           pb.TurnTrigger_TURN_TRIGGER_BRIEF,
		core.TriggerOperator:        pb.TurnTrigger_TURN_TRIGGER_OPERATOR,
		core.TriggerHandoff:         pb.TurnTrigger_TURN_TRIGGER_HANDOFF,
		core.TriggerJobFinished:     pb.TurnTrigger_TURN_TRIGGER_JOB_FINISHED,
		core.TriggerSchedule:        pb.TurnTrigger_TURN_TRIGGER_SCHEDULE,
		core.TriggerMessage:         pb.TurnTrigger_TURN_TRIGGER_MESSAGE,
		core.TriggerSubagentSettled: pb.TurnTrigger_TURN_TRIGGER_SUBAGENT_SETTLED,
		core.TriggerReminder:        pb.TurnTrigger_TURN_TRIGGER_REMINDER,
	})
	turnStatuses = newEnum(map[core.TurnStatus]pb.TurnStatus{
		core.TurnRunning:     pb.TurnStatus_TURN_STATUS_RUNNING,
		core.TurnCompleted:   pb.TurnStatus_TURN_STATUS_COMPLETED,
		core.TurnInterrupted: pb.TurnStatus_TURN_STATUS_INTERRUPTED,
		core.TurnFailed:      pb.TurnStatus_TURN_STATUS_FAILED,
	})
	itemStatuses = newEnum(map[core.ItemStatus]pb.ItemStatus{
		core.ItemRunning:  pb.ItemStatus_ITEM_STATUS_RUNNING,
		core.ItemFinished: pb.ItemStatus_ITEM_STATUS_FINISHED,
		core.ItemFailed:   pb.ItemStatus_ITEM_STATUS_FAILED,
	})
	harnesses = newEnum(map[core.Harness]pb.Harness{
		"":              pb.Harness_HARNESS_UNSPECIFIED,
		core.ClaudeCode: pb.Harness_HARNESS_CLAUDE_CODE,
		core.Codex:      pb.Harness_HARNESS_CODEX,
	})
	deliveries = newEnum(map[core.Delivery]pb.Delivery{
		"":                    pb.Delivery_DELIVERY_UNSPECIFIED,
		core.DeliveryQueued:   pb.Delivery_DELIVERY_QUEUED,
		core.DeliverySteered:  pb.Delivery_DELIVERY_STEERED,
		core.DeliveryNewTurn:  pb.Delivery_DELIVERY_NEW_TURN,
		core.DeliveryConsumed: pb.Delivery_DELIVERY_CONSUMED,
		core.DeliveryRejected: pb.Delivery_DELIVERY_REJECTED,
	})
	rejections = newEnum(map[core.Rejection]pb.Rejection{
		"":                          pb.Rejection_REJECTION_UNSPECIFIED,
		core.RejectAgentGone:        pb.Rejection_REJECTION_AGENT_GONE,
		core.RejectFanoutLimit:      pb.Rejection_REJECTION_FANOUT_LIMIT,
		core.RejectAgentStopping:    pb.Rejection_REJECTION_AGENT_STOPPING,
		core.RejectRunStopping:      pb.Rejection_REJECTION_RUN_STOPPING,
		core.RejectAttemptsExceeded: pb.Rejection_REJECTION_ATTEMPTS_EXHAUSTED,
	})
	jobOutcomes = newEnum(map[core.JobState]pb.JobOutcome{
		core.JobSucceeded: pb.JobOutcome_JOB_OUTCOME_SUCCEEDED,
		core.JobFailed:    pb.JobOutcome_JOB_OUTCOME_FAILED,
		core.JobCancelled: pb.JobOutcome_JOB_OUTCOME_CANCELLED,
		core.JobLost:      pb.JobOutcome_JOB_OUTCOME_LOST,
	})
	refusals = newEnum(map[core.Refusal]pb.Refusal{
		"":                         pb.Refusal_REFUSAL_UNSPECIFIED,
		core.RefuseRunNotFound:     pb.Refusal_REFUSAL_RUN_NOT_FOUND,
		core.RefuseAgentNotFound:   pb.Refusal_REFUSAL_AGENT_NOT_FOUND,
		core.RefuseNotPermitted:    pb.Refusal_REFUSAL_NOT_PERMITTED,
		core.RefuseRunNotRunning:   pb.Refusal_REFUSAL_RUN_NOT_RUNNING,
		core.RefuseFanoutLimit:     pb.Refusal_REFUSAL_FANOUT_LIMIT,
		core.RefuseAgentIsLead:     pb.Refusal_REFUSAL_AGENT_IS_LEAD,
		core.RefuseAgentNotLive:    pb.Refusal_REFUSAL_AGENT_NOT_LIVE,
		core.RefuseDeadlinePassed:  pb.Refusal_REFUSAL_DEADLINE_PASSED,
		core.RefuseRepositoryInUse: pb.Refusal_REFUSAL_REPOSITORY_IN_USE,
		core.RefuseAgentNotAllowed: pb.Refusal_REFUSAL_AGENT_NOT_ALLOWED,
	})
	startFailures = newEnum(map[core.StartFailure]pb.StartFailureReason{
		"":                              pb.StartFailureReason_START_FAILURE_REASON_UNSPECIFIED,
		core.StartAgentNotInstalled:     pb.StartFailureReason_START_FAILURE_REASON_AGENT_NOT_INSTALLED,
		core.StartNotSignedIn:           pb.StartFailureReason_START_FAILURE_REASON_NOT_SIGNED_IN,
		core.StartPluginUnavailable:     pb.StartFailureReason_START_FAILURE_REASON_PLUGIN_UNAVAILABLE,
		core.StartToolsUnavailable:      pb.StartFailureReason_START_FAILURE_REASON_TOOLS_UNAVAILABLE,
		core.StartWorkingDirUnavailable: pb.StartFailureReason_START_FAILURE_REASON_WORKING_DIRECTORY_UNAVAILABLE,
		core.StartTimedOut:              pb.StartFailureReason_START_FAILURE_REASON_TIMED_OUT,
		core.StartOther:                 pb.StartFailureReason_START_FAILURE_REASON_OTHER,
		core.StartIncompatibleVersions:  pb.StartFailureReason_START_FAILURE_REASON_INCOMPATIBLE_VERSIONS,
		core.StartStateLocationTooLong:  pb.StartFailureReason_START_FAILURE_REASON_STATE_LOCATION_TOO_LONG,
	})
	reportKinds = newEnum(map[core.ReportKind]pb.ReportKind{
		core.ReportProgress: pb.ReportKind_REPORT_KIND_PROGRESS,
		core.ReportFinal:    pb.ReportKind_REPORT_KIND_FINAL,
	})
	timerKinds = newEnum(map[core.TimerKind]pb.TimerKind{
		core.TimerRunDeadline:    pb.TimerKind_TIMER_KIND_RUN_DEADLINE,
		core.TimerStopGrace:      pb.TimerKind_TIMER_KIND_STOP_GRACE,
		core.TimerLaunchDeadline: pb.TimerKind_TIMER_KIND_LAUNCH_DEADLINE,
		core.TimerLeadIdle:       pb.TimerKind_TIMER_KIND_LEAD_IDLE,
	})
)

// RoleOut is the contract value of an agent's role, for the messages a program is started from.
func RoleOut(r core.AgentRole) pb.AgentRole { return roles.out(r) }

// RoleIn is the role a contract value names, or an error for the unspecified one or a value this
// build does not know.
func RoleIn(p pb.AgentRole) (core.AgentRole, error) {
	r, ok := roles.toCore[p]
	if !ok {
		return "", fmt.Errorf("no agent role for %v", p)
	}
	return r, nil
}
