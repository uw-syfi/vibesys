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
		return fmt.Sprintf("%s turn %d %s%s", agentLabel(c.Agent, c.Turn.GetAgentId()), c.Turn.GetOrdinal(), statusWord(c.Turn.GetStatus().String(), "TURN_STATUS_"), withPrefix(", ", usageWords(c.Turn.GetUsage())))
	case HandoffPosted:
		h := c.Message.GetHandoff()
		return fmt.Sprintf("%s handed off plan %d%s", agentLabel(c.Agent, c.Message.GetFromAgentId()), h.GetPlanNumber(), withPrefix(": ", oneLine(h.GetClaim(), 200)))
	case ReportPosted:
		r := c.Message.GetReport()
		return fmt.Sprintf("%s reported (%s, number %d): %s", agentLabel(c.Agent, c.Message.GetFromAgentId()), reportWord(r.GetKind()), r.GetSequence(), oneLine(r.GetHeadline(), 300))
	case RunStalled:
		return "run stalled: its lead is idle and nothing else is going on"
	case RunResumed:
		return "run is no longer stalled"
	case ReminderSent:
		return v.reminderText(c)
	case JobFinishedMessage:
		j := c.Message.GetJobFinished()
		return fmt.Sprintf("job %s %s%s", j.GetJobId(), statusWord(j.GetOutcome().String(), "JOB_OUTCOME_"), withPrefix(": ", oneLine(j.GetDetail(), 200)))
	case SubagentSettled:
		s := c.Message.GetSubagentSettled()
		return fmt.Sprintf("%s settled without a handoff (%s)%s", agentLabel(v.Agents[s.GetAgentId()], s.GetAgentId()), statusWord(s.GetStatus().String(), "AGENT_STATUS_"), withPrefix(": ", oneLine(s.GetDetail(), 200)))
	case MessageRejected:
		return v.rejectedText(c.Message)
	case JobMoved:
		return fmt.Sprintf("%s job %s %s", jobKind(c.Job), c.Job.GetId(), statusWord(c.Job.GetState().String(), "JOB_STATE_"))
	case Measured:
		m := c.Result.GetMeasure()
		return fmt.Sprintf("measured %s: %s = %g", shortCommit(c.Result.GetCommit()), m.GetMetric().GetName(), m.GetValue())
	}
	return ""
}

// reportWord says what kind a report is.
func reportWord(k pb.ReportKind) string {
	return statusWord(k.String(), "REPORT_KIND_")
}

// reminderText is the line for a reminder the orchestrator sent.
func (v *View) reminderText(c Change) string {
	switch b := c.Message.GetBody().(type) {
	case *pb.Message_RunLimitReached:
		return fmt.Sprintf("%s was told the run reached its %s and to finish", agentLabel(c.Agent, c.Message.GetToAgentId()), limitWord(b.RunLimitReached.GetLimit()))
	case *pb.Message_LeadIdleReminder:
		return fmt.Sprintf("%s was reminded: the run had stood still for %s", agentLabel(c.Agent, c.Message.GetToAgentId()), time.Duration(b.LeadIdleReminder.GetIdleSeconds())*time.Second)
	}
	return fmt.Sprintf("%s was reminded to post a handoff", agentLabel(c.Agent, c.Message.GetToAgentId()))
}

// rejectedText is the line for a message that did not get delivered. A message that already started
// a turn before it was refused reached the agent, so it is said to have been cut short, which is
// what happened to a steer when the run stopped under it.
func (v *View) rejectedText(m *pb.Message) string {
	to := agentLabel(v.Agents[m.GetToAgentId()], m.GetToAgentId())
	reason := statusWord(m.GetRejection().String(), "REJECTION_")
	if m.GetAttempts() > 0 {
		return fmt.Sprintf("your message to %s was cut short after its turn started: %s", to, reason)
	}
	return fmt.Sprintf("your message to %s was rejected: %s", to, reason)
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

// limitWord names a limit.
func limitWord(l pb.RunLimit) string {
	return statusWord(l.String(), "RUN_LIMIT_") + " limit"
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

// usageWords says what a usage cost: the dollars when the provider reports them, and the tokens when
// it reports those. What a provider does not report is left out, never shown as zero, and a usage
// with nothing in it is the empty string.
func usageWords(u *pb.Usage) string {
	if u == nil {
		return ""
	}
	var parts []string
	if u.CostUsd != nil {
		parts = append(parts, fmt.Sprintf("cost $%.4f", u.GetCostUsd()))
	}
	for _, t := range []struct {
		n    *uint64
		word string
	}{{u.InputTokens, "in"}, {u.CacheReadTokens, "cached"}, {u.OutputTokens, "out"}} {
		if t.n != nil {
			parts = append(parts, tokenWords(*t.n)+" "+t.word)
		}
	}
	return strings.Join(parts, ", ")
}

// tokenWords is a token count in the fewest digits that keep it readable: 950, 12.3k, 1.42M.
func tokenWords(n uint64) string {
	switch {
	case n < 1000:
		return fmt.Sprint(n)
	case n < 1_000_000:
		return fmt.Sprintf("%.1fk", float64(n)/1e3)
	}
	return fmt.Sprintf("%.2fM", float64(n)/1e6)
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
