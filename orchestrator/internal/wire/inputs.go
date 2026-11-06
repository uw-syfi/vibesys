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
			Deadline: moment(r.GetDeadline(), &errs), Lead: specIn(r.GetLead(), &errs), Brief: r.GetBrief(),
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
		c.Body = core.PostHandoff{Handoff: handoffIn(b.PostHandoff.GetHandoff())}
	case *pb.Command_PostReport:
		c.Body = core.PostReport{Report: reportIn(b.PostReport.GetReport())}
	default:
		errs.errs = append(errs.errs, describe("command", m.GetBody()))
	}
	return c, errs.err()
}

// ParseSidecarEvent reads one event of a session's stream. A delta is not a fact, and comes back on
// its own for the shell to pass to watchers.
func ParseSidecarEvent(from core.Session, data []byte) (core.SidecarFact, *pb.Delta, error) {
	var m pb.SidecarEvent
	if err := parse(data, &m); err != nil {
		return core.SidecarFact{}, nil, err
	}
	var errs errorList
	f := core.SidecarFact{Session: from, Offset: m.GetOffset()}
	switch b := m.GetBody().(type) {
	case *pb.SidecarEvent_Opened:
		o := b.Opened
		f.Body = core.ConversationOpened{
			ConversationID: o.GetProviderConversationId(), Capabilities: capabilitiesIn(o.GetCapabilities()),
			Worktree: o.GetWorktree(),
		}
	case *pb.SidecarEvent_Item:
		f.Body = sidecarItemIn(b.Item, &errs)
	case *pb.SidecarEvent_Delta:
		return core.SidecarFact{}, deltaOut(from, b.Delta), nil
	case *pb.SidecarEvent_TurnEnded:
		t := b.TurnEnded
		f.Body = core.TurnEnded{
			Turn: core.TurnID(t.GetTurnId()), Status: turnStatuses.in(t.GetStatus(), &errs), Usage: usageIn(t.GetUsage()),
		}
	case *pb.SidecarEvent_Steer:
		f.Body = core.SteerOutcome{Message: core.MessageID(b.Steer.GetMessageId()), Accepted: b.Steer.GetAccepted()}
	case *pb.SidecarEvent_Usage:
		f.Body = core.UsageReported{Turn: core.TurnID(b.Usage.GetTurnId()), Usage: usageIn(b.Usage.GetUsage())}
	case *pb.SidecarEvent_Failed:
		f.Body = core.SessionFailed{Detail: b.Failed.GetDetail(), StartFailure: startFailures.in(b.Failed.GetStartFailureReason(), &errs)}
	case *pb.SidecarEvent_Closed:
		f.Body = core.SessionClosed{}
	default:
		errs.errs = append(errs.errs, describe("sidecar event", m.GetBody()))
	}
	return f, nil, errs.err()
}

func sidecarItemIn(i *pb.SidecarItem, errs *errorList) core.ItemObserved {
	out := core.ItemObserved{
		ID: core.ItemID(i.GetId()), Turn: core.TurnID(i.GetTurnId()), Status: itemStatuses.in(i.GetStatus(), errs),
	}
	switch b := i.GetBody().(type) {
	case *pb.SidecarItem_Assistant:
		out.Body = core.Assistant{Text: b.Assistant.GetText()}
	case *pb.SidecarItem_Reasoning:
		out.Body = core.Reasoning{Text: b.Reasoning.GetText()}
	case *pb.SidecarItem_Prompt:
		out.Body = core.Prompt{Text: b.Prompt.GetText()}
	case *pb.SidecarItem_Tool:
		out.Body = toolIn(b.Tool)
	}
	return out
}

func deltaOut(from core.Session, d *pb.SidecarDelta) *pb.Delta {
	out := &pb.Delta{AgentId: string(from.Agent), ItemId: d.GetItemId()}
	switch b := d.GetBody().(type) {
	case *pb.SidecarDelta_Text:
		out.Body = &pb.Delta_Text{Text: b.Text}
	case *pb.SidecarDelta_CommandOutput:
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
			Session: sessionRef(b.Session), RunId: string(b.Run), Repository: b.Repository, Task: b.Task, Role: roles.out(b.Role),
			CliLocation: locationOut(b.Location),
			Harness:     harnesses.out(b.Spec.Harness), Model: string(b.Spec.Model), ReasoningEffort: string(b.Spec.Effort),
			ResumeConversationId: b.ConversationID, Worktree: b.Worktree,
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
			Session: session(l.GetSession()), Run: core.RunID(l.GetRunId()), Repository: l.GetRepository(), Task: l.GetTask(),
			Location: locationIn(l.GetCliLocation()),
			Role:     roles.in(l.GetRole(), &errs),
			Spec: core.AgentSpec{
				Harness: harnesses.in(l.GetHarness(), &errs), Model: core.ModelName(l.GetModel()),
				Effort: core.EffortName(l.GetReasoningEffort()),
			},
			ConversationID: l.GetResumeConversationId(), Worktree: l.GetWorktree(),
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
