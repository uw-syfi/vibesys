package clientcli

import (
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// statusWord turns an enum name such as RUN_STATUS_ENDED into the word a person reads, "ended".
func statusWord(name, prefix string) string {
	return strings.ToLower(strings.ReplaceAll(strings.TrimPrefix(name, prefix), "_", " "))
}

// clock formats a time of day, or blanks of the same width when the time is unknown.
func clock(t time.Time, zone *time.Location) string {
	if t.IsZero() {
		return "        "
	}
	return t.In(zone).Format("15:04:05")
}

// oneLine collapses white space and cuts the text to at most limit runes.
func oneLine(text string, limit int) string {
	text = strings.Join(strings.Fields(text), " ")
	if utf8.RuneCountInString(text) <= limit {
		return text
	}
	runes := []rune(text)
	return string(runes[:limit-1]) + "..."
}

func agentLabel(a *pb.Agent, id string) string {
	if a == nil {
		return id
	}
	return statusWord(a.GetRole().String(), "AGENT_ROLE_") + " " + a.GetId()
}

// Describe is the line a change is shown as, with its time of day. It reads the view only for what
// the change refers to, such as the lead's failure when the run failed.
func (v *View) Describe(c Change, zone *time.Location) string {
	return clock(c.At, zone) + "  " + v.text(c)
}

func (v *View) text(c Change) string {
	switch c.Kind {
	case RunMoved:
		return v.runText(c.Run)
	case AgentStarted:
		a := c.Agent
		return fmt.Sprintf("%s started (%s%s)", agentLabel(a, ""), strings.ToLower(strings.TrimPrefix(a.GetHarness().String(), "HARNESS_")), withPrefix(", model ", a.GetModel()))
	case AgentEnded:
		a := c.Agent
		return fmt.Sprintf("%s %s%s", agentLabel(a, ""), statusWord(a.GetStatus().String(), "AGENT_STATUS_"), withPrefix(": ", endDetail(a, 300)))
	case TurnEnded:
		return fmt.Sprintf("%s turn %d %s%s", agentLabel(c.Agent, c.Turn.GetAgentId()), c.Turn.GetOrdinal(), statusWord(c.Turn.GetStatus().String(), "TURN_STATUS_"), cost(c.Turn.GetUsage()))
	case HandoffPosted:
		h := c.Message.GetHandoff()
		return fmt.Sprintf("%s handed off plan %d%s", agentLabel(c.Agent, c.Message.GetFromAgentId()), h.GetPlanNumber(), withPrefix(": ", oneLine(h.GetClaim(), 200)))
	case ReportPosted:
		return fmt.Sprintf("%s reported: %s", agentLabel(c.Agent, c.Message.GetFromAgentId()), oneLine(c.Message.GetReport().GetHeadline(), 300))
	case JobFinishedMessage:
		j := c.Message.GetJobFinished()
		return fmt.Sprintf("job %s %s%s", j.GetJobId(), statusWord(j.GetOutcome().String(), "JOB_OUTCOME_"), withPrefix(": ", oneLine(j.GetDetail(), 200)))
	case SubagentSettled:
		s := c.Message.GetSubagentSettled()
		return fmt.Sprintf("%s settled without a handoff (%s)%s", agentLabel(v.Agents[s.GetAgentId()], s.GetAgentId()), statusWord(s.GetStatus().String(), "AGENT_STATUS_"), withPrefix(": ", oneLine(s.GetDetail(), 200)))
	case MessageRejected:
		return fmt.Sprintf("your message to %s was rejected: %s", agentLabel(v.Agents[c.Message.GetToAgentId()], c.Message.GetToAgentId()), statusWord(c.Message.GetRejection().String(), "REJECTION_"))
	case JobMoved:
		return fmt.Sprintf("%s job %s %s", jobKind(c.Job), c.Job.GetId(), statusWord(c.Job.GetState().String(), "JOB_STATE_"))
	case Measured:
		m := c.Result.GetMeasure()
		return fmt.Sprintf("measured %s: %s = %g", shortCommit(c.Result.GetCommit()), m.GetMetric().GetName(), m.GetValue())
	}
	return ""
}

func (v *View) runText(r *pb.Run) string {
	switch r.GetStatus() {
	case pb.RunStatus_RUN_STATUS_FAILED:
		return "run failed" + withPrefix(": ", v.failureDetail(r))
	case pb.RunStatus_RUN_STATUS_STOPPING, pb.RunStatus_RUN_STATUS_ENDED:
		return fmt.Sprintf("run %s%s", statusWord(r.GetStatus().String(), "RUN_STATUS_"), withPrefix(" by ", stopReason(r)))
	}
	return "run " + statusWord(r.GetStatus().String(), "RUN_STATUS_")
}

func stopReason(r *pb.Run) string {
	if r.GetStopReason() == pb.RunStopReason_RUN_STOP_REASON_UNSPECIFIED {
		return ""
	}
	return statusWord(r.GetStopReason().String(), "RUN_STOP_REASON_")
}

// failureDetail is why a run failed: what its lead ended with, else the stop reason.
func (v *View) failureDetail(r *pb.Run) string {
	if detail := endDetail(v.Agents[r.GetLeadAgentId()], 500); detail != "" {
		return detail
	}
	return stopReason(r)
}

// endDetail is why an agent's session ended, with the reason it never opened when it did not.
func endDetail(a *pb.Agent, limit int) string {
	detail := oneLine(a.GetEndDetail(), limit)
	reason := startFailureWords(a.GetStartFailureReason())
	switch {
	case reason == "":
		return detail
	case detail == "":
		return reason
	}
	return detail + " (" + reason + ")"
}

// startFailureWords names why a session never opened, or is empty when it did not fail to.
func startFailureWords(r pb.StartFailureReason) string {
	if r == pb.StartFailureReason_START_FAILURE_REASON_UNSPECIFIED {
		return ""
	}
	return statusWord(r.String(), "START_FAILURE_REASON_")
}

func withPrefix(prefix, text string) string {
	if text == "" {
		return ""
	}
	return prefix + text
}

func cost(u *pb.Usage) string {
	if u == nil || u.CostUsd == nil {
		return ""
	}
	return fmt.Sprintf(", cost $%.4f", u.GetCostUsd())
}

func jobKind(j *cliv1.Job) string {
	switch j.GetSpec().GetSettings().(type) {
	case *cliv1.JobSpec_Check:
		return "check"
	case *cliv1.JobSpec_Measure:
		return "measure"
	case *cliv1.JobSpec_Command:
		return "command"
	}
	return "unknown"
}

func shortCommit(commit string) string {
	if len(commit) > 8 {
		return commit[:8]
	}
	return commit
}
