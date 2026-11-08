package wire

import (
	"fmt"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The functions below give the daemon's service the contract's messages for what it reads, so that
// it never builds one field by field.

// RunMessage returns a run as the contract states it.
func RunMessage(r core.Run) *pb.Run { return runOut(r) }

// AgentMessage returns an agent as the contract states it.
func AgentMessage(a core.Agent) *pb.Agent { return agentOut(a) }

// TurnMessage returns a turn as the contract states it.
func TurnMessage(t core.Turn) *pb.Turn { return turnOut(t) }

// TurnFromMessage reads a turn given as the contract states it: the inverse of TurnMessage.
func TurnFromMessage(t *pb.Turn) (core.Turn, error) {
	var errs errorList
	out := turnIn(t, &errs)
	return out, errs.err()
}

// MailboxMessage returns a message between agents (a brief, a handoff, a report) as the contract
// states it.
func MailboxMessage(m core.Message) *pb.Message { return messageOut(m) }

// CheckInMessage returns a check-in as the contract states it.
func CheckInMessage(c core.CheckIn) *pb.CheckIn { return checkInOut(c) }

// CheckInFromMessage reads a check-in given as the contract states it: the inverse of CheckInMessage.
func CheckInFromMessage(c *pb.CheckIn) (core.CheckIn, error) {
	var errs errorList
	out := checkInIn(c, &errs)
	return out, errs.err()
}

// ItemMessage returns an item as the contract states it.
func ItemMessage(i core.Item) *pb.Item { return itemOut(i) }

// ReceiptMessage returns a receipt, with the sequence of the event that holds it.
func ReceiptMessage(r core.Receipt, sequence uint64) *pb.Receipt { return receiptOut(r, sequence) }

// ChangeMessage returns what a watcher receives for a committed event: its public part, and nothing
// the host keeps for itself.
func ChangeMessage(sequence uint64, e core.Event) (*pb.RunChange, error) {
	out := &pb.RunChange{Sequence: sequence, At: when(e.At), RunId: string(e.Run)}
	switch b := e.Body.(type) {
	case core.Run:
		out.Body = &pb.RunChange_Run{Run: runOut(b)}
	case core.Agent:
		out.Body = &pb.RunChange_Agent{Agent: agentOut(b)}
	case core.Turn:
		out.Body = &pb.RunChange_Turn{Turn: turnOut(b)}
	case core.Item:
		out.Body = &pb.RunChange_Item{Item: itemOut(b)}
	case core.Message:
		out.Body = &pb.RunChange_Message{Message: messageOut(b)}
	case core.Receipt:
		out.Body = &pb.RunChange_Receipt{Receipt: receiptOut(b, sequence)}
	case core.CheckIn:
		out.Body = &pb.RunChange_CheckIn{CheckIn: checkInOut(b)}
	case core.RunSettingsUpdated:
		out.Body = &pb.RunChange_RunUpdated{RunUpdated: settingsUpdatedOut(b)}
	default:
		return nil, describe("event", e.Body)
	}
	return out, nil
}

// ParseCommandMessage reads a command that already arrived as a message. It holds the command to
// the same rules as ParseCommand, which is what a command from the wire goes through.
func ParseCommandMessage(m *pb.Command, caller core.Caller) (core.Command, error) {
	data, err := proto.Marshal(m)
	if err != nil {
		return core.Command{}, fmt.Errorf("command %s: %w", m.GetId(), err)
	}
	return ParseCommand(data, caller)
}

// Validate holds a message that arrived in memory to the contract: its rules, and no field this
// build does not know.
func Validate(m proto.Message) error { return check(m) }

// MailboxFromMessage reads a message given as the contract states it: the inverse of MailboxMessage.
func MailboxFromMessage(m *pb.Message) (core.Message, error) {
	var errs errorList
	out := messageIn(m, &errs)
	return out, errs.err()
}

// Prompt gives the words an agent is sent for a message.
type Prompt func(core.Message) (string, error)

// SessionCommandFor returns what a session is sent for an effect. It reports false for the
// effects that are not commands to a live session: launching a session and arming a timer.
//
// A turn or a steer carries the words of its message and nothing of the message: prompt words it
// each time, so a delivery that is retried sends the words the first one did.
func SessionCommandFor(e core.Effect, prompt Prompt) (*pb.SessionCommand, bool, error) {
	out := &pb.SessionCommand{EffectId: string(e.ID())}
	switch b := e.(type) {
	case core.StartTurn:
		words, err := prompt(b.Message)
		if err != nil {
			return nil, false, fmt.Errorf("effect %s: %w", e.ID(), err)
		}
		out.Body = &pb.SessionCommand_StartTurn{StartTurn: &pb.SessionStartTurn{TurnId: string(b.Turn), Prompt: words}}
		return out, true, nil
	case core.Steer:
		words, err := prompt(b.Message)
		if err != nil {
			return nil, false, fmt.Errorf("effect %s: %w", e.ID(), err)
		}
		out.Body = &pb.SessionCommand_Steer{Steer: &pb.SessionSteer{
			TurnId: string(b.Turn), MessageId: string(b.Message.ID), Prompt: words,
		}}
		return out, true, nil
	}
	data, err := EncodeEffect(e)
	if err != nil {
		return nil, false, err
	}
	var m pb.Effect
	if err := proto.Unmarshal(data, &m); err != nil {
		return nil, false, fmt.Errorf("effect %s: %w", e.ID(), err)
	}
	switch b := m.GetBody().(type) {
	case *pb.Effect_Interrupt:
		out.Body = &pb.SessionCommand_Interrupt{Interrupt: b.Interrupt}
	case *pb.Effect_CloseSession:
		out.Body = &pb.SessionCommand_Close{Close: b.CloseSession}
	default:
		return nil, false, nil
	}
	return out, true, nil
}
