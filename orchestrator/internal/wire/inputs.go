package wire

import (
	"fmt"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

func specIn(s *pb.AgentSpec, errs *errorList) core.AgentSpec {
	return core.AgentSpec{
		Harness: harnesses.in(s.GetHarness(), errs), Model: core.ModelName(s.GetModel()),
		Effort: core.EffortName(s.GetReasoningEffort()),
	}
}

func choicesIn(in []*pb.AllowedAgent, errs *errorList) []core.AgentChoice {
	var out []core.AgentChoice
	for _, c := range in {
		out = append(out, core.AgentChoice{Harness: harnesses.in(c.GetHarness(), errs), Model: core.ModelName(c.GetModel())})
	}
	return out
}

func choicesOut(in []core.AgentChoice) []*pb.AllowedAgent {
	var out []*pb.AllowedAgent
	for _, c := range in {
		out = append(out, &pb.AllowedAgent{Harness: harnesses.out(c.Harness), Model: string(c.Model)})
	}
	return out
}

// ParseCommand reads a command. The caller comes from the shell, which knows who is connected: the
// contract has no field for it, so a caller cannot claim to be someone else.
func ParseCommand(data []byte, caller core.Caller) (core.Command, error) {
	var m pb.Command
	if err := parse(data, &m); err != nil {
		return core.Command{}, err
	}
	sum, err := digest(&m)
	if err != nil {
		return core.Command{}, err
	}
	var errs errorList
	c := core.Command{ID: core.CommandID(m.GetId()), Digest: sum, Caller: caller}
	switch b := m.GetBody().(type) {
	case *pb.Command_StartRun:
		r := b.StartRun
		c.Body = core.StartRun{
			Repository: r.GetRepository(), Task: r.GetTask(), FanoutLimit: r.GetFanoutLimit(),
			MaxTime: r.GetMaxTime().AsDuration(), MaxCostUSD: r.GetMaxCostUsd(), SummaryEvery: r.GetSummaryEvery().AsDuration(),
			Lead: specIn(r.GetLead(), &errs), Brief: r.GetBrief(), Allow: choicesIn(r.GetAllowAgents(), &errs),
		}
	case *pb.Command_StopRun:
		c.Body = core.StopRun{Run: core.RunID(b.StopRun.GetRunId())}
	case *pb.Command_SpawnAgent:
		s := b.SpawnAgent
		c.Body = core.SpawnAgent{
			Run: core.RunID(s.GetRunId()), Role: roles.in(s.GetRole(), &errs), Spec: specIn(s.GetSpec(), &errs),
			PlanNumber: s.GetPlanNumber(), Brief: s.GetBrief(),
		}
	case *pb.Command_StopAgent:
		c.Body = core.StopAgent{Agent: core.AgentID(b.StopAgent.GetAgentId())}
	case *pb.Command_SendMessage:
		c.Body = core.SendMessage{Agent: core.AgentID(b.SendMessage.GetAgentId()), Text: b.SendMessage.GetText()}
	case *pb.Command_PostHandoff:
		c.Body = core.PostHandoff{Handoff: handoffIn(b.PostHandoff.GetHandoff(), &errs)}
	case *pb.Command_PostReport:
		c.Body = core.PostReport{Report: reportIn(b.PostReport.GetReport(), &errs)}
	default:
		errs.errs = append(errs.errs, describe("command", m.GetBody()))
	}
	return c, errs.err()
}

// ParseSessionEvent reads one event of a session's stream. A delta is not a fact, and comes back on
// its own for the shell to pass to watchers.
func ParseSessionEvent(from core.Session, data []byte) (core.SessionFact, *pb.Delta, error) {
	var m pb.SessionEvent
	if err := parse(data, &m); err != nil {
		return core.SessionFact{}, nil, err
	}
	var errs errorList
	f := core.SessionFact{Session: from, Offset: m.GetOffset()}
	switch b := m.GetBody().(type) {
	case *pb.SessionEvent_Opened:
		o := b.Opened
		f.Body = core.SessionOpened{
			ResumeToken: string(o.GetResumeToken()), Capabilities: capabilitiesIn(o.GetCapabilities()),
		}
	case *pb.SessionEvent_Item:
		f.Body = sessionItemIn(b.Item, &errs)
	case *pb.SessionEvent_Delta:
		return core.SessionFact{}, deltaOut(from, b.Delta), nil
	case *pb.SessionEvent_TurnEnded:
		t := b.TurnEnded
		f.Body = core.TurnEnded{
			Turn: core.TurnID(t.GetTurnId()), Status: turnStatuses.in(t.GetStatus(), &errs), Usage: usageIn(t.GetUsage()),
		}
	case *pb.SessionEvent_Steer:
		f.Body = core.SteerOutcome{Message: core.MessageID(b.Steer.GetMessageId()), Accepted: b.Steer.GetAccepted()}
	case *pb.SessionEvent_Usage:
		f.Body = core.UsageReported{Turn: core.TurnID(b.Usage.GetTurnId()), Usage: usageIn(b.Usage.GetUsage())}
	case *pb.SessionEvent_Failed:
		f.Body = core.SessionFailed{Detail: b.Failed.GetDetail(), StartFailure: startFailures.in(b.Failed.GetStartFailureReason(), &errs)}
	case *pb.SessionEvent_Closed:
		f.Body = core.SessionClosed{}
	default:
		errs.errs = append(errs.errs, describe("session event", m.GetBody()))
	}
	return f, nil, errs.err()
}

func sessionItemIn(i *pb.SessionItem, errs *errorList) core.ItemObserved {
	out := core.ItemObserved{
		ID: core.ItemID(i.GetId()), Turn: core.TurnID(i.GetTurnId()), Status: itemStatuses.in(i.GetStatus(), errs),
	}
	switch b := i.GetBody().(type) {
	case *pb.SessionItem_Assistant:
		out.Body = core.Assistant{Text: b.Assistant.GetText()}
	case *pb.SessionItem_Reasoning:
		out.Body = core.Reasoning{Text: b.Reasoning.GetText()}
	case *pb.SessionItem_Prompt:
		out.Body = core.Prompt{Text: b.Prompt.GetText()}
	case *pb.SessionItem_Tool:
		out.Body = toolIn(b.Tool)
	}
	return out
}

func deltaOut(from core.Session, d *pb.SessionDelta) *pb.Delta {
	out := &pb.Delta{AgentId: string(from.Agent), ItemId: d.GetItemId()}
	switch b := d.GetBody().(type) {
	case *pb.SessionDelta_Text:
		out.Body = &pb.Delta_Text{Text: b.Text}
	case *pb.SessionDelta_CommandOutput:
		out.Body = &pb.Delta_CommandOutput{CommandOutput: b.CommandOutput}
	}
	return out
}

// EncodeEffect gives one effect its form in the outbox.
func EncodeEffect(e core.Effect) ([]byte, error) {
	out := &pb.Effect{Id: string(e.ID())}
	switch b := e.(type) {
	case core.LaunchSession:
		out.Body = &pb.Effect_LaunchSession{LaunchSession: &pb.LaunchSession{
			Session: sessionRef(b.Session), RunId: string(b.Run), Role: roles.out(b.Role),
			Harness: harnesses.out(b.Spec.Harness), Model: string(b.Spec.Model), ReasoningEffort: string(b.Spec.Effort),
			ResumeToken: []byte(b.ResumeToken),
		}}
	case core.StartTurn:
		out.Body = &pb.Effect_StartTurn{StartTurn: &pb.StartTurn{
			Session: sessionRef(b.Session), TurnId: string(b.Turn), Message: messageOut(b.Message),
		}}
	case core.Steer:
		out.Body = &pb.Effect_Steer{Steer: &pb.Steer{
			Session: sessionRef(b.Session), TurnId: string(b.Turn), Message: messageOut(b.Message),
		}}
	case core.Interrupt:
		out.Body = &pb.Effect_Interrupt{Interrupt: &pb.Interrupt{Session: sessionRef(b.Session), TurnId: string(b.Turn)}}
	case core.CloseSession:
		out.Body = &pb.Effect_CloseSession{CloseSession: &pb.CloseSession{Session: sessionRef(b.Session), Force: b.Force}}
	case core.ReleaseCheckouts:
		out.Body = &pb.Effect_ReleaseCheckouts{ReleaseCheckouts: &pb.ReleaseCheckouts{RunId: string(b.Run)}}
	case core.ArmTimer:
		timer := &pb.ArmTimer{
			Kind: timerKinds.out(b.Kind), RunId: string(b.Run), FireAt: when(b.FireAt),
		}
		if b.Session != (core.Session{}) {
			timer.Session = sessionRef(b.Session)
		}
		out.Body = &pb.Effect_ArmTimer{ArmTimer: timer}
	default:
		return nil, describe("effect", e)
	}
	return proto.Marshal(out)
}

// DecodeEffect reads an effect back from the outbox.
func DecodeEffect(data []byte) (core.Effect, error) {
	var m pb.Effect
	if err := parse(data, &m); err != nil {
		return nil, err
	}
	var errs errorList
	var e core.Effect
	switch b := m.GetBody().(type) {
	case *pb.Effect_LaunchSession:
		l := b.LaunchSession
		e = core.LaunchSession{
			Session: session(l.GetSession()), Run: core.RunID(l.GetRunId()),
			Role: roles.in(l.GetRole(), &errs),
			Spec: core.AgentSpec{
				Harness: harnesses.in(l.GetHarness(), &errs), Model: core.ModelName(l.GetModel()),
				Effort: core.EffortName(l.GetReasoningEffort()),
			},
			ResumeToken: string(l.GetResumeToken()),
		}
	case *pb.Effect_StartTurn:
		s := b.StartTurn
		e = core.StartTurn{
			Session: session(s.GetSession()), Turn: core.TurnID(s.GetTurnId()), Message: messageIn(s.GetMessage(), &errs),
		}
	case *pb.Effect_Steer:
		s := b.Steer
		e = core.Steer{
			Session: session(s.GetSession()), Turn: core.TurnID(s.GetTurnId()), Message: messageIn(s.GetMessage(), &errs),
		}
	case *pb.Effect_Interrupt:
		e = core.Interrupt{Session: session(b.Interrupt.GetSession()), Turn: core.TurnID(b.Interrupt.GetTurnId())}
	case *pb.Effect_CloseSession:
		e = core.CloseSession{Session: session(b.CloseSession.GetSession()), Force: b.CloseSession.GetForce()}
	case *pb.Effect_ReleaseCheckouts:
		e = core.ReleaseCheckouts{Run: core.RunID(b.ReleaseCheckouts.GetRunId())}
	case *pb.Effect_ArmTimer:
		t := b.ArmTimer
		timer := core.ArmTimer{Kind: timerKinds.in(t.GetKind(), &errs), Run: core.RunID(t.GetRunId()), FireAt: moment(t.GetFireAt(), &errs)}
		if t.GetSession() != nil {
			timer.Session = session(t.GetSession())
		}
		e = timer
	default:
		errs.errs = append(errs.errs, describe("effect", m.GetBody()))
	}
	if errs.err() != nil {
		return nil, errs.err()
	}
	if got := string(e.ID()); got != m.GetId() {
		return nil, fmt.Errorf("effect %s: its id does not match its body, which is %s", m.GetId(), got)
	}
	return e, nil
}
