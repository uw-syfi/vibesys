package attachview

import (
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// StatusWord turns an enum name such as RUN_STATUS_ENDED into the word a person reads, "ended".
func StatusWord(name, prefix string) string {
	return strings.ToLower(strings.ReplaceAll(strings.TrimPrefix(name, prefix), "_", " "))
}

// Clock formats a time of day, or blanks of the same width when the time is unknown.
func Clock(t time.Time, zone *time.Location) string {
	if t.IsZero() {
		return "        "
	}
	return t.In(zone).Format("15:04:05")
}

// OneLine collapses white space and cuts the text to at most limit runes.
func OneLine(text string, limit int) string {
	text = strings.Join(strings.Fields(Sanitize(text)), " ")
	if utf8.RuneCountInString(text) <= limit {
		return text
	}
	runes := []rune(text)
	return string(runes[:limit-1]) + "..."
}

// AgentLabel is how a message that acts on an agent names it: its role and its id, the id being what
// a person passes to `run steer --agent`.
func AgentLabel(a *pb.Agent, id string) string {
	if a == nil {
		return id
	}
	return StatusWord(a.GetRole().String(), "AGENT_ROLE_") + " " + a.GetId()
}

// Describe is the line a change is shown as, with its time of day. It reads the view only for what
// the change refers to, such as the lead's failure when the run failed.
func (v *View) Describe(c Change, zone *time.Location) string {
	return Clock(c.At, zone) + "  " + v.text(c)
}

func (v *View) text(c Change) string {
	switch c.Kind {
	case RunMoved:
		return v.runText(c.Run)
	case AgentStarted:
		a := c.Agent
		return fmt.Sprintf("%s started (%s%s)", v.AgentName(a.GetId()), strings.ToLower(strings.TrimPrefix(a.GetHarness().String(), "HARNESS_")), WithPrefix(", model ", a.GetModel()))
	case AgentEnded:
		a := c.Agent
		return fmt.Sprintf("%s %s%s", v.AgentName(a.GetId()), StatusWord(a.GetStatus().String(), "AGENT_STATUS_"), WithPrefix(": ", EndDetail(a, 300)))
	case TurnEnded:
		return turnEndWords(v.AgentName(c.Turn.GetAgentId()), c.Turn, -1)
	case HandoffPosted:
		h := c.Message.GetHandoff()
		return fmt.Sprintf("%s handed off%s%s", v.AgentName(c.Message.GetFromAgentId()), planWords(v.handoffPlan(c.Message)), WithPrefix(": ", OneLine(h.GetClaim(), 200)))
	case ReportPosted:
		r := c.Message.GetReport()
		return fmt.Sprintf("%s reported (%s, number %d): %s", v.AgentName(c.Message.GetFromAgentId()), ReportWord(r.GetKind()), r.GetSequence(), OneLine(r.GetHeadline(), 300))
	case RunStalled:
		return "run stalled: its lead is idle and nothing else is going on"
	case RunResumed:
		return "run is no longer stalled"
	case ReminderSent:
		return v.reminderText(c)
	case JobFinishedMessage:
		j := c.Message.GetJobFinished()
		return fmt.Sprintf("job %s %s%s", j.GetJobId(), StatusWord(j.GetOutcome().String(), "JOB_OUTCOME_"), WithPrefix(": ", OneLine(j.GetDetail(), 200)))
	case SubagentSettled:
		s := c.Message.GetSubagentSettled()
		return fmt.Sprintf("%s settled without a handoff (%s)%s", v.AgentName(s.GetAgentId()), StatusWord(s.GetStatus().String(), "AGENT_STATUS_"), WithPrefix(": ", OneLine(s.GetDetail(), 200)))
	case MessageRejected:
		return v.rejectedText(c.Message)
	case JobMoved:
		return fmt.Sprintf("%s job %s %s", JobKind(c.Job), c.Job.GetId(), StatusWord(c.Job.GetState().String(), "JOB_STATE_"))
	case Measured:
		m := c.Result.GetMeasure()
		return fmt.Sprintf("measured %s: %s = %g", ShortCommit(c.Result.GetCommit()), m.GetMetric().GetName(), m.GetValue())
	}
	return ""
}

// handoffPlan is the plan a handoff is about, or zero when nothing says. The agent fills in the
// number when it posts the handoff, and a planner usually cannot: the CLI gives its plan a number
// when the plan is made, and the post_handoff input does not carry it back. Zero is then "not
// said", never plan 0, so it falls back to the plan the lead gave the agent when it started it.
func (v *View) handoffPlan(m *pb.Message) uint32 {
	if n := m.GetHandoff().GetPlanNumber(); n != 0 {
		return n
	}
	return v.Agents[m.GetFromAgentId()].GetPlanNumber()
}

// planWords says which plan, or nothing when no plan is known.
func planWords(n uint32) string {
	if n == 0 {
		return ""
	}
	return fmt.Sprintf(" plan %d", n)
}

// turnEndWords is the line for a turn that ended, with the tool calls it made when they are known (a
// negative count says they are not). Input tokens are left out on purpose: a turn's figure adds up
// every model call in it, so it reads as a context size and is not one.
func turnEndWords(who string, t *pb.Turn, tools int) string {
	var parts []string
	if tools >= 0 {
		parts = append(parts, fmt.Sprintf("%d tool %s", tools, plural(uint32(tools), "call", "calls")))
	}
	if u := t.GetUsage(); u != nil {
		if u.CostUsd != nil {
			parts = append(parts, fmt.Sprintf("cost $%.4f", u.GetCostUsd()))
		}
		if u.OutputTokens != nil {
			parts = append(parts, tokenWords(u.GetOutputTokens())+" out")
		}
	}
	return fmt.Sprintf("%s turn %d %s%s", who, t.GetOrdinal(), StatusWord(t.GetStatus().String(), "TURN_STATUS_"), WithPrefix(", ", strings.Join(parts, ", ")))
}

// ReportWord says what kind a report is.
func ReportWord(k pb.ReportKind) string {
	return StatusWord(k.String(), "REPORT_KIND_")
}

// reminderText is the line for a reminder the orchestrator sent.
func (v *View) reminderText(c Change) string {
	switch b := c.Message.GetBody().(type) {
	case *pb.Message_RunLimitReached:
		return fmt.Sprintf("%s was told the run reached its %s and to finish", v.AgentName(c.Message.GetToAgentId()), LimitWord(b.RunLimitReached.GetLimit()))
	case *pb.Message_LeadIdleReminder:
		return fmt.Sprintf("%s was reminded: the run had stood still for %s", v.AgentName(c.Message.GetToAgentId()), time.Duration(b.LeadIdleReminder.GetIdleSeconds())*time.Second)
	}
	return fmt.Sprintf("%s was reminded to post a handoff", v.AgentName(c.Message.GetToAgentId()))
}

// rejectedText is the line for a message that did not get delivered. A message that already started
// a turn before it was refused reached the agent, so it is said to have been cut short, which is
// what happened to a steer when the run stopped under it.
func (v *View) rejectedText(m *pb.Message) string {
	to := v.AgentName(m.GetToAgentId())
	reason := StatusWord(m.GetRejection().String(), "REJECTION_")
	if m.GetAttempts() > 0 {
		return fmt.Sprintf("your message to %s was cut short after its turn started: %s", to, reason)
	}
	return fmt.Sprintf("your message to %s was rejected: %s", to, reason)
}

func (v *View) runText(r *pb.Run) string {
	switch r.GetStatus() {
	case pb.RunStatus_RUN_STATUS_FAILED:
		return "run failed" + WithPrefix(": ", v.FailureDetail(r))
	case pb.RunStatus_RUN_STATUS_STOPPING, pb.RunStatus_RUN_STATUS_ENDED:
		return fmt.Sprintf("run %s%s", StatusWord(r.GetStatus().String(), "RUN_STATUS_"), WithPrefix(" by ", StopReason(r)))
	}
	return "run " + StatusWord(r.GetStatus().String(), "RUN_STATUS_")
}

// LimitWord names a limit.
func LimitWord(l pb.RunLimit) string {
	return StatusWord(l.String(), "RUN_LIMIT_") + " limit"
}

func StopReason(r *pb.Run) string {
	if r.GetStopReason() == pb.RunStopReason_RUN_STOP_REASON_UNSPECIFIED {
		return ""
	}
	return StatusWord(r.GetStopReason().String(), "RUN_STOP_REASON_")
}

// FailureDetail is why a run failed: what its lead ended with, else the stop reason.
func (v *View) FailureDetail(r *pb.Run) string {
	if detail := EndDetail(v.Agents[r.GetLeadAgentId()], 500); detail != "" {
		return detail
	}
	return StopReason(r)
}

// EndDetail is why an agent's session ended, with the reason it never opened when it did not.
func EndDetail(a *pb.Agent, limit int) string {
	detail := OneLine(a.GetEndDetail(), limit)
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
	return StatusWord(r.String(), "START_FAILURE_REASON_")
}

func WithPrefix(prefix, text string) string {
	if text == "" {
		return ""
	}
	return prefix + text
}

// UsageWords says what a usage cost: the dollars when the provider reports them, and the tokens when
// it reports those. What a provider does not report is left out, never shown as zero, and a usage
// with nothing in it is the empty string.
func UsageWords(u *pb.Usage) string {
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

func JobKind(j *cliv1.Job) string {
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

func ShortCommit(commit string) string {
	if len(commit) > 8 {
		return commit[:8]
	}
	return commit
}
