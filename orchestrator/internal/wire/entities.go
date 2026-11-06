package wire

import (
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

func runOut(r core.Run) *pb.Run {
	return &pb.Run{
		Id: string(r.ID), Repository: r.Repository, Task: r.Task, Status: runStatuses.out(r.Status),
		LeadAgentId: string(r.Lead), FanoutLimit: r.FanoutLimit, Deadline: when(r.Deadline),
		CreatedAt: when(r.CreatedAt), StopReason: stopReasons.out(r.StopReason), EndedAt: when(r.EndedAt),
		ReportsPosted: r.ReportsPosted, FinalReported: r.FinalReported, StalledSince: when(r.StalledSince),
		ActiveJobs: r.ActiveJobs, LeadReminders: r.LeadReminders, Usage: usageOut(r.Usage),
		AllowedAgents: choicesOut(r.Allowed), KeptCheckouts: checkoutsOut(r.Kept),
		NeverOpened: r.NeverOpened,
		MaxCostUsd:  limitOut(r.MaxCostUSD), LimitReached: runLimits.out(r.LimitReached), LimitReachedAt: when(r.LimitReachedAt),
	}
}

// runRecordOut is a run as it is stored: the public message, and what only the host keeps.
func runRecordOut(r core.Run) *pb.RunRecord {
	return &pb.RunRecord{Run: runOut(r), JobTool: jobToolOut(r.JobTool)}
}

// limitOut is the contract's cost limit: absent for none.
func limitOut(usd float64) *float64 {
	if usd == 0 {
		return nil
	}
	return &usd
}

func runRecordIn(r *pb.RunRecord, errs *errorList) core.Run {
	out := runIn(r.GetRun(), errs)
	out.JobTool = jobToolIn(r.GetJobTool())
	return out
}

func jobToolOut(l core.JobToolEndpoint) *pb.JobToolEndpoint {
	if l == (core.JobToolEndpoint{}) {
		return nil
	}
	return &pb.JobToolEndpoint{Executable: l.Executable, RuntimeDirectory: l.RuntimeDirectory}
}

func jobToolIn(l *pb.JobToolEndpoint) core.JobToolEndpoint {
	return core.JobToolEndpoint{Executable: l.GetExecutable(), RuntimeDirectory: l.GetRuntimeDirectory()}
}

func runIn(r *pb.Run, errs *errorList) core.Run {
	return core.Run{
		ID: core.RunID(r.GetId()), Repository: r.GetRepository(), Task: r.GetTask(),
		Status: runStatuses.in(r.GetStatus(), errs), Lead: core.AgentID(r.GetLeadAgentId()),
		FanoutLimit: r.GetFanoutLimit(), Deadline: moment(r.GetDeadline(), errs),
		CreatedAt: moment(r.GetCreatedAt(), errs), StopReason: stopReasons.in(r.GetStopReason(), errs),
		EndedAt:       moment(r.GetEndedAt(), errs),
		ReportsPosted: r.GetReportsPosted(), FinalReported: r.GetFinalReported(), StalledSince: moment(r.GetStalledSince(), errs),
		ActiveJobs: r.GetActiveJobs(), LeadReminders: r.GetLeadReminders(), Usage: usageIn(r.GetUsage()),
		Allowed: choicesIn(r.GetAllowedAgents(), errs), Kept: checkoutsIn(r.GetKeptCheckouts()),
		NeverOpened: r.GetNeverOpened(),
		MaxCostUSD:  r.GetMaxCostUsd(), LimitReached: runLimits.in(r.GetLimitReached(), errs), LimitReachedAt: moment(r.GetLimitReachedAt(), errs),
	}
}

func checkoutsOut(in []core.Checkout) []*pb.KeptCheckout {
	var out []*pb.KeptCheckout
	for _, c := range in {
		out = append(out, &pb.KeptCheckout{AgentId: string(c.Agent), Location: c.Location})
	}
	return out
}

func checkoutsIn(in []*pb.KeptCheckout) []core.Checkout {
	var out []core.Checkout
	for _, c := range in {
		out = append(out, core.Checkout{Agent: core.AgentID(c.GetAgentId()), Location: c.GetLocation()})
	}
	return out
}

func capabilitiesOut(c core.Capabilities) *pb.Capabilities {
	if c == (core.Capabilities{}) {
		return nil
	}
	return &pb.Capabilities{Steers: c.Steers, Resumes: c.Resumes}
}

func capabilitiesIn(c *pb.Capabilities) core.Capabilities {
	return core.Capabilities{Steers: c.GetSteers(), Resumes: c.GetResumes()}
}

func agentOut(a core.Agent) *pb.Agent {
	return &pb.Agent{
		Id: string(a.ID), RunId: string(a.Run), ParentAgentId: string(a.Parent), Role: roles.out(a.Role),
		Harness: harnesses.out(a.Spec.Harness), Model: string(a.Spec.Model),
		ReasoningEffort: string(a.Spec.Effort), PlanNumber: a.PlanNumber,
		Status:       agentStatuses.out(a.Status),
		Capabilities: capabilitiesOut(a.Capabilities), StartedAt: when(a.StartedAt), EndedAt: when(a.EndedAt),
		Session: a.Session, StopStage: stopStages.out(a.Stop), HandedOff: a.HandedOff,
		SessionFailures: a.SessionFailures, EndDetail: a.EndDetail, HandoffReminded: a.HandoffReminded,
		StartFailureReason: startFailures.out(a.StartFailure), Usage: usageOut(a.Usage),
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
		PlanNumber: a.GetPlanNumber(), Status: agentStatuses.in(a.GetStatus(), errs),
		Capabilities: capabilitiesIn(a.GetCapabilities()),
		StartedAt:    moment(a.GetStartedAt(), errs), EndedAt: moment(a.GetEndedAt(), errs), Session: a.GetSession(),
		Stop: stopStages.in(a.GetStopStage(), errs), HandedOff: a.GetHandedOff(), SessionFailures: a.GetSessionFailures(),
		EndDetail: a.GetEndDetail(), StartFailure: startFailures.in(a.GetStartFailureReason(), errs), HandoffReminded: a.GetHandoffReminded(), Usage: usageIn(a.GetUsage()),
	}
}

// agentRecordOut is an agent as it is stored: the public message, and what only the host keeps.
func agentRecordOut(a core.Agent) *pb.AgentRecord {
	return &pb.AgentRecord{Agent: agentOut(a), ResumeToken: []byte(a.ResumeToken)}
}

func agentRecordIn(a *pb.AgentRecord, errs *errorList) core.Agent {
	out := agentIn(a.GetAgent(), errs)
	out.ResumeToken = string(a.GetResumeToken())
	return out
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
	out := &pb.ToolCall{Output: c.Output, ExitCode: c.ExitCode, IsError: c.IsError}
	switch u := c.Use.(type) {
	case core.ShellUse:
		out.Call = &pb.ToolCall_Shell{Shell: &pb.ShellCall{Command: u.Command}}
	case core.ReadUse:
		out.Call = &pb.ToolCall_Read{Read: &pb.ReadCall{Target: u.Target}}
	case core.WriteUse:
		out.Call = &pb.ToolCall_Write{Write: &pb.WriteCall{Target: u.Target}}
	case core.EditUse:
		out.Call = &pb.ToolCall_Edit{Edit: &pb.EditCall{Targets: u.Targets}}
	case core.SearchUse:
		out.Call = &pb.ToolCall_Search{Search: &pb.SearchCall{Pattern: u.Pattern}}
	case core.OtherUse:
		out.Call = &pb.ToolCall_Other{Other: &pb.OtherCall{Name: u.Name, Text: u.Text}}
	}
	return out
}

func toolIn(c *pb.ToolCall) core.ToolCall {
	out := core.ToolCall{Output: c.GetOutput(), ExitCode: c.ExitCode, IsError: c.GetIsError()}
	switch u := c.GetCall().(type) {
	case *pb.ToolCall_Shell:
		out.Use = core.ShellUse{Command: u.Shell.GetCommand()}
	case *pb.ToolCall_Read:
		out.Use = core.ReadUse{Target: u.Read.GetTarget()}
	case *pb.ToolCall_Write:
		out.Use = core.WriteUse{Target: u.Write.GetTarget()}
	case *pb.ToolCall_Edit:
		out.Use = core.EditUse{Targets: u.Edit.GetTargets()}
	case *pb.ToolCall_Search:
		out.Use = core.SearchUse{Pattern: u.Search.GetPattern()}
	case *pb.ToolCall_Other:
		out.Use = core.OtherUse{Name: u.Other.GetName(), Text: u.Other.GetText()}
	}
	return out
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
	out := &pb.Handoff{PlanNumber: h.PlanNumber, Claim: h.Claim, Learned: h.Learned, Next: h.Next, OpenItems: h.OpenItems}
	switch b := h.Body.(type) {
	case core.WorkerCandidate:
		out.Body = &pb.Handoff_Worker{Worker: &pb.WorkerHandoff{Outcome: &pb.WorkerHandoff_Candidate{Candidate: &pb.Candidate{
			Commit: string(b.Commit), ParentCommit: string(b.ParentCommit), Branch: b.Branch, Checks: b.Checks, Measurement: b.Measurement,
		}}}}
	case core.WorkerNoCandidate:
		out.Body = &pb.Handoff_Worker{Worker: &pb.WorkerHandoff{Outcome: &pb.WorkerHandoff_NoCandidate{NoCandidate: &pb.NoCandidate{Blocker: b.Blocker}}}}
	case core.PlannerPlan:
		out.Body = &pb.Handoff_Planner{Planner: &pb.PlannerHandoff{
			Plan: b.Plan, Confidence: b.Confidence, Unestablished: b.Unestablished, DecidingMeasurement: b.DecidingMeasurement,
		}}
	case core.JudgeFinding:
		out.Body = &pb.Handoff_Judge{Judge: &pb.JudgeHandoff{Commit: string(b.Commit), Verdict: verdicts.out(b.Verdict), Reasons: b.Reasons}}
	case core.CombinerResult:
		out.Body = &pb.Handoff_Combiner{Combiner: &pb.CombinerHandoff{
			Commit: string(b.Commit), Branch: b.Branch, Applied: b.Applied, Unapplied: b.Unapplied, Checks: b.Checks, Measurement: b.Measurement,
		}}
	case core.ProfilerFindings:
		out.Body = &pb.Handoff_Profiler{Profiler: &pb.ProfilerHandoff{
			Question: b.Question, Revision: string(b.Revision), Answer: b.Answer, Evidence: b.Evidence, Unsupported: b.Unsupported, Informs: b.Informs,
		}}
	}
	return out
}

func handoffIn(h *pb.Handoff, errs *errorList) core.Handoff {
	out := core.Handoff{PlanNumber: h.GetPlanNumber(), Claim: h.GetClaim(), Learned: h.GetLearned(), Next: h.GetNext(), OpenItems: h.GetOpenItems()}
	switch b := h.GetBody().(type) {
	case *pb.Handoff_Worker:
		switch o := b.Worker.GetOutcome().(type) {
		case *pb.WorkerHandoff_Candidate:
			c := o.Candidate
			out.Body = core.WorkerCandidate{
				Commit: core.Commit(c.GetCommit()), ParentCommit: core.Commit(c.GetParentCommit()), Branch: c.GetBranch(),
				Checks: c.GetChecks(), Measurement: c.GetMeasurement(),
			}
		case *pb.WorkerHandoff_NoCandidate:
			out.Body = core.WorkerNoCandidate{Blocker: o.NoCandidate.GetBlocker()}
		default:
			errs.addf("a worker handoff has no outcome")
		}
	case *pb.Handoff_Planner:
		p := b.Planner
		out.Body = core.PlannerPlan{Plan: p.GetPlan(), Confidence: p.GetConfidence(), Unestablished: p.GetUnestablished(), DecidingMeasurement: p.GetDecidingMeasurement()}
	case *pb.Handoff_Judge:
		j := b.Judge
		out.Body = core.JudgeFinding{Commit: core.Commit(j.GetCommit()), Verdict: verdicts.in(j.GetVerdict(), errs), Reasons: j.GetReasons()}
	case *pb.Handoff_Combiner:
		c := b.Combiner
		out.Body = core.CombinerResult{
			Commit: core.Commit(c.GetCommit()), Branch: c.GetBranch(), Applied: c.GetApplied(), Unapplied: c.GetUnapplied(),
			Checks: c.GetChecks(), Measurement: c.GetMeasurement(),
		}
	case *pb.Handoff_Profiler:
		p := b.Profiler
		out.Body = core.ProfilerFindings{
			Question: p.GetQuestion(), Revision: core.Commit(p.GetRevision()), Answer: p.GetAnswer(), Evidence: p.GetEvidence(),
			Unsupported: p.GetUnsupported(), Informs: p.GetInforms(),
		}
	default:
		errs.addf("a handoff has no body of a role")
	}
	return out
}

func reportOut(r core.Report) *pb.Report {
	return &pb.Report{
		Kind: reportKinds.out(r.Kind), Sequence: r.Sequence, Headline: r.Headline, Changed: r.Changed, InFlight: r.InFlight, Decisions: r.Decisions,
		OpenForUser: r.OpenForUser,
	}
}

func reportIn(r *pb.Report, errs *errorList) core.Report {
	return core.Report{
		Kind: reportKinds.in(r.GetKind(), errs), Sequence: r.GetSequence(), Headline: r.GetHeadline(), Changed: r.GetChanged(), InFlight: r.GetInFlight(), Decisions: r.GetDecisions(),
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
	case core.LeadBrief:
		out.Body = &pb.Message_LeadBrief{LeadBrief: &pb.LeadBrief{Task: b.Task, FanoutLimit: b.FanoutLimit, Steering: b.Steering}}
	case core.Handoff:
		out.Body = &pb.Message_Handoff{Handoff: handoffOut(b)}
	case core.Report:
		out.Body = &pb.Message_Report{Report: reportOut(b)}
	case core.JobFinished:
		out.Body = &pb.Message_JobFinished{JobFinished: &pb.JobFinished{
			JobId: string(b.Job), Outcome: jobOutcomes.out(b.Outcome),
			SubmittedByAgentId: string(b.SubmittedBy), Detail: b.Detail,
		}}
	case core.HandoffReminder:
		out.Body = &pb.Message_HandoffReminder{HandoffReminder: &pb.HandoffReminder{}}
	case core.LeadIdleReminder:
		out.Body = &pb.Message_LeadIdleReminder{LeadIdleReminder: &pb.LeadIdleReminder{
			IdleSeconds: uint32(b.Idle / time.Second), SubagentsStarted: b.Subagents, WorkersStarted: b.Workers, JudgesStarted: b.Judges,
		}}
	case core.RunLimitReached:
		out.Body = &pb.Message_RunLimitReached{RunLimitReached: &pb.RunLimitReached{
			Limit: runLimits.out(b.Limit), ElapsedSeconds: uint32(b.Elapsed / time.Second), SpentUsd: b.Spent,
			MaxCostUsd: limitOut(b.MaxCost), GraceSeconds: uint32(b.Grace / time.Second),
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
	case *pb.Message_LeadBrief:
		out.Body = core.LeadBrief{Task: b.LeadBrief.GetTask(), FanoutLimit: b.LeadBrief.GetFanoutLimit(), Steering: b.LeadBrief.GetSteering()}
	case *pb.Message_Handoff:
		out.Body = handoffIn(b.Handoff, errs)
	case *pb.Message_Report:
		out.Body = reportIn(b.Report, errs)
	case *pb.Message_JobFinished:
		out.Body = core.JobFinished{
			Job: core.JobID(b.JobFinished.GetJobId()), Outcome: jobOutcomes.in(b.JobFinished.GetOutcome(), errs),
			SubmittedBy: core.AgentID(b.JobFinished.GetSubmittedByAgentId()), Detail: b.JobFinished.GetDetail(),
		}
	case *pb.Message_HandoffReminder:
		out.Body = core.HandoffReminder{}
	case *pb.Message_LeadIdleReminder:
		out.Body = core.LeadIdleReminder{
			Idle:      time.Duration(b.LeadIdleReminder.GetIdleSeconds()) * time.Second,
			Subagents: b.LeadIdleReminder.GetSubagentsStarted(), Workers: b.LeadIdleReminder.GetWorkersStarted(),
			Judges: b.LeadIdleReminder.GetJudgesStarted(),
		}
	case *pb.Message_RunLimitReached:
		l := b.RunLimitReached
		out.Body = core.RunLimitReached{
			Limit: runLimits.in(l.GetLimit(), errs), Elapsed: time.Duration(l.GetElapsedSeconds()) * time.Second,
			Spent: l.SpentUsd, MaxCost: l.GetMaxCostUsd(), Grace: time.Duration(l.GetGraceSeconds()) * time.Second,
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
		CommandId: string(r.Command), Refusal: refusals.out(r.Refusal),
		RunId: string(r.Run), AgentId: string(r.Agent), MessageId: string(r.Message),
		Delivery: deliveries.out(r.Delivery), Sequence: sequence, AllowedAgents: choicesOut(r.Allowed),
	}
}

func receiptIn(r *pb.Receipt, errs *errorList) core.Receipt {
	return core.Receipt{
		Command: core.CommandID(r.GetCommandId()),
		Refusal: refusals.in(r.GetRefusal(), errs), Run: core.RunID(r.GetRunId()), Agent: core.AgentID(r.GetAgentId()),
		Message: core.MessageID(r.GetMessageId()), Delivery: deliveries.in(r.GetDelivery(), errs),
		Allowed: choicesIn(r.GetAllowedAgents(), errs),
	}
}

// receiptRecordOut is a receipt as it is stored: the public message, and what only the host keeps.
func receiptRecordOut(r core.Receipt, sequence uint64) *pb.ReceiptRecord {
	return &pb.ReceiptRecord{Receipt: receiptOut(r, sequence), CommandDigest: string(r.Digest)}
}

func receiptRecordIn(r *pb.ReceiptRecord, errs *errorList) core.Receipt {
	out := receiptIn(r.GetReceipt(), errs)
	out.Digest = core.Digest(r.GetCommandDigest())
	return out
}

// EncodeEvent gives one event its committed form. The store assigns the sequence.
func EncodeEvent(sequence uint64, e core.Event) ([]byte, error) {
	out := &pb.Event{
		Sequence: sequence, RunId: string(e.Run), CommandId: string(e.Command), At: when(e.At),
	}
	if s := e.Source; s != nil {
		out.Source = &pb.SessionSource{AgentId: string(s.Session.Agent), Session: s.Session.N, Offset: s.Offset}
	}
	switch b := e.Body.(type) {
	case core.Run:
		out.Body = &pb.Event_RunUpdated{RunUpdated: runRecordOut(b)}
	case core.Agent:
		out.Body = &pb.Event_AgentUpdated{AgentUpdated: agentRecordOut(b)}
	case core.Turn:
		out.Body = &pb.Event_TurnUpdated{TurnUpdated: turnOut(b)}
	case core.Item:
		out.Body = &pb.Event_ItemUpdated{ItemUpdated: itemOut(b)}
	case core.Message:
		out.Body = &pb.Event_MessageUpdated{MessageUpdated: messageOut(b)}
	case core.Receipt:
		out.Body = &pb.Event_ReceiptRecorded{ReceiptRecorded: receiptRecordOut(b, sequence)}
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
		e.Source = &core.SessionSource{
			Session: core.Session{Agent: core.AgentID(s.GetAgentId()), N: s.GetSession()}, Offset: s.GetOffset(),
		}
	}
	switch b := m.GetBody().(type) {
	case *pb.Event_RunUpdated:
		e.Body = runRecordIn(b.RunUpdated, &errs)
	case *pb.Event_AgentUpdated:
		e.Body = agentRecordIn(b.AgentUpdated, &errs)
	case *pb.Event_TurnUpdated:
		e.Body = turnIn(b.TurnUpdated, &errs)
	case *pb.Event_ItemUpdated:
		e.Body = itemIn(b.ItemUpdated, &errs)
	case *pb.Event_MessageUpdated:
		e.Body = messageIn(b.MessageUpdated, &errs)
	case *pb.Event_ReceiptRecorded:
		e.Body = receiptRecordIn(b.ReceiptRecorded, &errs)
	default:
		errs.errs = append(errs.errs, describe("event", m.GetBody()))
	}
	return m.GetSequence(), e, errs.err()
}
