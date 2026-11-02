package wire

import (
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

func runOut(r core.Run) *pb.Run {
	return &pb.Run{
		Id: string(r.ID), Repository: r.Repository, Task: r.Task, Status: runStatuses.out(r.Status),
		LeadAgentId: string(r.Lead), FanoutLimit: r.FanoutLimit, Deadline: when(r.Deadline),
		CreatedAt: when(r.CreatedAt), StopReason: stopReasons.out(r.StopReason), EndedAt: when(r.EndedAt),
	}
}

func runIn(r *pb.Run, errs *errorList) core.Run {
	return core.Run{
		ID: core.RunID(r.GetId()), Repository: r.GetRepository(), Task: r.GetTask(),
		Status: runStatuses.in(r.GetStatus(), errs), Lead: core.AgentID(r.GetLeadAgentId()),
		FanoutLimit: r.GetFanoutLimit(), Deadline: moment(r.GetDeadline(), errs),
		CreatedAt: moment(r.GetCreatedAt(), errs), StopReason: stopReasons.in(r.GetStopReason(), errs),
		EndedAt: moment(r.GetEndedAt(), errs),
	}
}

func capabilitiesOut(c core.Capabilities) *pb.Capabilities {
	if c == (core.Capabilities{}) {
		return nil
	}
	return &pb.Capabilities{StreamsText: c.StreamsText, Steers: c.Steers, Resumes: c.Resumes}
}

func capabilitiesIn(c *pb.Capabilities) core.Capabilities {
	return core.Capabilities{StreamsText: c.GetStreamsText(), Steers: c.GetSteers(), Resumes: c.GetResumes()}
}

func agentOut(a core.Agent) *pb.Agent {
	return &pb.Agent{
		Id: string(a.ID), RunId: string(a.Run), ParentAgentId: string(a.Parent), Role: roles.out(a.Role),
		Harness: harnesses.out(a.Spec.Harness), Model: string(a.Spec.Model),
		ReasoningEffort: string(a.Spec.Effort), PlanNumber: a.PlanNumber, Worktree: a.Worktree,
		Status: agentStatuses.out(a.Status), ProviderConversationId: a.ConversationID,
		Capabilities: capabilitiesOut(a.Capabilities), StartedAt: when(a.StartedAt), EndedAt: when(a.EndedAt),
		Session: a.Session, StopStage: stopStages.out(a.Stop), HandedOff: a.HandedOff,
		SessionFailures: a.SessionFailures, EndDetail: a.EndDetail,
	}
}

func agentIn(a *pb.Agent, errs *errorList) core.Agent {
	return core.Agent{
		ID: core.AgentID(a.GetId()), Run: core.RunID(a.GetRunId()), Parent: core.AgentID(a.GetParentAgentId()),
		Role: roles.in(a.GetRole(), errs),
		Spec: core.AgentSpec{
			Harness: harnesses.in(a.GetHarness(), errs), Model: core.ModelName(a.GetModel()),
			Effort: core.EffortName(a.GetReasoningEffort()),
		},
		PlanNumber: a.GetPlanNumber(), Worktree: a.GetWorktree(), Status: agentStatuses.in(a.GetStatus(), errs),
		ConversationID: a.GetProviderConversationId(), Capabilities: capabilitiesIn(a.GetCapabilities()),
		StartedAt: moment(a.GetStartedAt(), errs), EndedAt: moment(a.GetEndedAt(), errs), Session: a.GetSession(),
		Stop: stopStages.in(a.GetStopStage(), errs), HandedOff: a.GetHandedOff(), SessionFailures: a.GetSessionFailures(),
		EndDetail: a.GetEndDetail(),
	}
}

func usageOut(u core.Usage) *pb.Usage {
	if u == (core.Usage{}) {
		return nil
	}
	return &pb.Usage{
		InputTokens: u.InputTokens, CacheReadTokens: u.CacheReadTokens, CacheCreationTokens: u.CacheCreationTokens,
		OutputTokens: u.OutputTokens, ReasoningTokens: u.ReasoningTokens, CostUsd: u.CostUSD,
	}
}

func usageIn(u *pb.Usage) core.Usage {
	if u == nil {
		return core.Usage{}
	}
	return core.Usage{
		InputTokens: u.InputTokens, CacheReadTokens: u.CacheReadTokens, CacheCreationTokens: u.CacheCreationTokens,
		OutputTokens: u.OutputTokens, ReasoningTokens: u.ReasoningTokens, CostUSD: u.CostUsd,
	}
}

func turnOut(t core.Turn) *pb.Turn {
	return &pb.Turn{
		Id: string(t.ID), AgentId: string(t.Agent), Ordinal: t.Ordinal, Trigger: triggers.out(t.Trigger),
		TriggerMessageId: string(t.Message), Status: turnStatuses.out(t.Status), Usage: usageOut(t.Usage),
		StartedAt: when(t.StartedAt), EndedAt: when(t.EndedAt),
	}
}

func turnIn(t *pb.Turn, errs *errorList) core.Turn {
	return core.Turn{
		ID: core.TurnID(t.GetId()), Agent: core.AgentID(t.GetAgentId()), Ordinal: t.GetOrdinal(),
		Trigger: triggers.in(t.GetTrigger(), errs), Message: core.MessageID(t.GetTriggerMessageId()),
		Status: turnStatuses.in(t.GetStatus(), errs), Usage: usageIn(t.GetUsage()),
		StartedAt: moment(t.GetStartedAt(), errs), EndedAt: moment(t.GetEndedAt(), errs),
	}
}

func toolOut(c core.ToolCall) *pb.ToolCall {
	return &pb.ToolCall{
		Tool: c.Tool, ProviderCallId: c.ProviderCallID, ArgsJson: c.ArgsJSON, Output: c.Output,
		ExitCode: c.ExitCode, IsError: c.IsError,
	}
}

func toolIn(c *pb.ToolCall) core.ToolCall {
	return core.ToolCall{
		Tool: c.GetTool(), ProviderCallID: c.GetProviderCallId(), ArgsJSON: c.GetArgsJson(), Output: c.GetOutput(),
		ExitCode: c.ExitCode, IsError: c.GetIsError(),
	}
}

func itemOut(i core.Item) *pb.Item {
	out := &pb.Item{
		Id: string(i.ID), TurnId: string(i.Turn), AgentId: string(i.Agent), Ordinal: i.Ordinal,
		Status: itemStatuses.out(i.Status), At: when(i.At),
	}
	switch b := i.Body.(type) {
	case core.Assistant:
		out.Body = &pb.Item_Assistant{Assistant: &pb.Text{Text: b.Text}}
	case core.Reasoning:
		out.Body = &pb.Item_Reasoning{Reasoning: &pb.Text{Text: b.Text}}
	case core.Prompt:
		out.Body = &pb.Item_Prompt{Prompt: &pb.Text{Text: b.Text}}
	case core.ToolCall:
		out.Body = &pb.Item_Tool{Tool: toolOut(b)}
	}
	return out
}

func itemIn(i *pb.Item, errs *errorList) core.Item {
	out := core.Item{
		ID: core.ItemID(i.GetId()), Turn: core.TurnID(i.GetTurnId()), Agent: core.AgentID(i.GetAgentId()),
		Ordinal: i.GetOrdinal(), Status: itemStatuses.in(i.GetStatus(), errs), At: moment(i.GetAt(), errs),
	}
	switch b := i.GetBody().(type) {
	case *pb.Item_Assistant:
		out.Body = core.Assistant{Text: b.Assistant.GetText()}
	case *pb.Item_Reasoning:
		out.Body = core.Reasoning{Text: b.Reasoning.GetText()}
	case *pb.Item_Prompt:
		out.Body = core.Prompt{Text: b.Prompt.GetText()}
	case *pb.Item_Tool:
		out.Body = toolIn(b.Tool)
	}
	return out
}

func handoffOut(h core.Handoff) *pb.Handoff {
	return &pb.Handoff{
		PlanNumber: h.PlanNumber, HasCandidate: h.HasCandidate, Claim: h.Claim, Commit: string(h.Commit),
		ParentCommit: string(h.ParentCommit), Branch: h.Branch, Checks: h.Checks, Measurement: h.Measurement,
		Learned: h.Learned, Next: h.Next, OpenItems: h.OpenItems,
	}
}

func handoffIn(h *pb.Handoff) core.Handoff {
	return core.Handoff{
		PlanNumber: h.GetPlanNumber(), HasCandidate: h.GetHasCandidate(), Claim: h.GetClaim(),
		Commit: core.Commit(h.GetCommit()), ParentCommit: core.Commit(h.GetParentCommit()), Branch: h.GetBranch(),
		Checks: h.GetChecks(), Measurement: h.GetMeasurement(), Learned: h.GetLearned(), Next: h.GetNext(),
		OpenItems: h.GetOpenItems(),
	}
}

func reportOut(r core.Report) *pb.Report {
	return &pb.Report{
		Headline: r.Headline, Changed: r.Changed, InFlight: r.InFlight, Decisions: r.Decisions,
		OpenForUser: r.OpenForUser,
	}
}

func reportIn(r *pb.Report) core.Report {
	return core.Report{
		Headline: r.GetHeadline(), Changed: r.GetChanged(), InFlight: r.GetInFlight(), Decisions: r.GetDecisions(),
		OpenForUser: r.GetOpenForUser(),
	}
}

func messageOut(m core.Message) *pb.Message {
	out := &pb.Message{
		Id: string(m.ID), RunId: string(m.Run), FromAgentId: string(m.From), ToAgentId: string(m.To),
		Delivery: deliveries.out(m.Delivery), At: when(m.At), Rejection: rejections.out(m.Rejection),
		Attempts: m.Attempts,
	}
	switch b := m.Body.(type) {
	case core.Text:
		out.Body = &pb.Message_Text{Text: b.Text}
	case core.Brief:
		out.Body = &pb.Message_Brief{Brief: b.Text}
	case core.Handoff:
		out.Body = &pb.Message_Handoff{Handoff: handoffOut(b)}
	case core.Report:
		out.Body = &pb.Message_Report{Report: reportOut(b)}
	case core.JobFinished:
		out.Body = &pb.Message_JobFinished{JobFinished: &pb.JobFinished{
			JobId: string(b.Job), Outcome: jobOutcomes.out(b.Outcome),
			SubmittedByAgentId: string(b.SubmittedBy), Detail: b.Detail,
		}}
	case core.SubagentSettled:
		out.Body = &pb.Message_SubagentSettled{SubagentSettled: &pb.SubagentSettled{
			AgentId: string(b.Agent), Status: agentStatuses.out(b.Status), Detail: b.Detail,
		}}
	}
	return out
}

func messageIn(m *pb.Message, errs *errorList) core.Message {
	out := core.Message{
		ID: core.MessageID(m.GetId()), Run: core.RunID(m.GetRunId()), From: core.AgentID(m.GetFromAgentId()),
		To: core.AgentID(m.GetToAgentId()), Delivery: deliveries.in(m.GetDelivery(), errs), At: moment(m.GetAt(), errs),
		Rejection: rejections.in(m.GetRejection(), errs), Attempts: m.GetAttempts(),
	}
	switch b := m.GetBody().(type) {
	case *pb.Message_Text:
		out.Body = core.Text{Text: b.Text}
	case *pb.Message_Brief:
		out.Body = core.Brief{Text: b.Brief}
	case *pb.Message_Handoff:
		out.Body = handoffIn(b.Handoff)
	case *pb.Message_Report:
		out.Body = reportIn(b.Report)
	case *pb.Message_JobFinished:
		out.Body = core.JobFinished{
			Job: core.JobID(b.JobFinished.GetJobId()), Outcome: jobOutcomes.in(b.JobFinished.GetOutcome(), errs),
			SubmittedBy: core.AgentID(b.JobFinished.GetSubmittedByAgentId()), Detail: b.JobFinished.GetDetail(),
		}
	case *pb.Message_SubagentSettled:
		out.Body = core.SubagentSettled{
			Agent: core.AgentID(b.SubagentSettled.GetAgentId()), Status: agentStatuses.in(b.SubagentSettled.GetStatus(), errs),
			Detail: b.SubagentSettled.GetDetail(),
		}
	}
	return out
}

func receiptOut(r core.Receipt, sequence uint64) *pb.Receipt {
	return &pb.Receipt{
		CommandId: string(r.Command), CommandDigest: string(r.Digest), Refusal: refusals.out(r.Refusal),
		RunId: string(r.Run), AgentId: string(r.Agent), MessageId: string(r.Message),
		Delivery: deliveries.out(r.Delivery), Sequence: sequence,
	}
}

func receiptIn(r *pb.Receipt, errs *errorList) core.Receipt {
	return core.Receipt{
		Command: core.CommandID(r.GetCommandId()), Digest: core.Digest(r.GetCommandDigest()),
		Refusal: refusals.in(r.GetRefusal(), errs), Run: core.RunID(r.GetRunId()), Agent: core.AgentID(r.GetAgentId()),
		Message: core.MessageID(r.GetMessageId()), Delivery: deliveries.in(r.GetDelivery(), errs),
	}
}

// EncodeEvent gives one event its committed form. The store assigns the sequence.
func EncodeEvent(sequence uint64, e core.Event) ([]byte, error) {
	out := &pb.Event{
		Sequence: sequence, RunId: string(e.Run), CommandId: string(e.Command), At: when(e.At),
	}
	if s := e.Source; s != nil {
		out.Source = &pb.SidecarSource{AgentId: string(s.Session.Agent), Session: s.Session.N, Offset: s.Offset}
	}
	switch b := e.Body.(type) {
	case core.Run:
		out.Body = &pb.Event_RunUpdated{RunUpdated: runOut(b)}
	case core.Agent:
		out.Body = &pb.Event_AgentUpdated{AgentUpdated: agentOut(b)}
	case core.Turn:
		out.Body = &pb.Event_TurnUpdated{TurnUpdated: turnOut(b)}
	case core.Item:
		out.Body = &pb.Event_ItemUpdated{ItemUpdated: itemOut(b)}
	case core.Message:
		out.Body = &pb.Event_MessageUpdated{MessageUpdated: messageOut(b)}
	case core.Receipt:
		out.Body = &pb.Event_ReceiptRecorded{ReceiptRecorded: receiptOut(b, sequence)}
	default:
		return nil, describe("event", e.Body)
	}
	return proto.Marshal(out)
}

// DecodeEvent reads a committed event back.
func DecodeEvent(data []byte) (uint64, core.Event, error) {
	var m pb.Event
	if err := parse(data, &m); err != nil {
		return 0, core.Event{}, err
	}
	var errs errorList
	e := core.Event{
		Run: core.RunID(m.GetRunId()), Command: core.CommandID(m.GetCommandId()), At: moment(m.GetAt(), &errs),
	}
	if s := m.GetSource(); s != nil {
		e.Source = &core.SidecarSource{
			Session: core.Session{Agent: core.AgentID(s.GetAgentId()), N: s.GetSession()}, Offset: s.GetOffset(),
		}
	}
	switch b := m.GetBody().(type) {
	case *pb.Event_RunUpdated:
		e.Body = runIn(b.RunUpdated, &errs)
	case *pb.Event_AgentUpdated:
		e.Body = agentIn(b.AgentUpdated, &errs)
	case *pb.Event_TurnUpdated:
		e.Body = turnIn(b.TurnUpdated, &errs)
	case *pb.Event_ItemUpdated:
		e.Body = itemIn(b.ItemUpdated, &errs)
	case *pb.Event_MessageUpdated:
		e.Body = messageIn(b.MessageUpdated, &errs)
	case *pb.Event_ReceiptRecorded:
		e.Body = receiptIn(b.ReceiptRecorded, &errs)
	default:
		errs.errs = append(errs.errs, describe("event", m.GetBody()))
	}
	return m.GetSequence(), e, errs.err()
}
