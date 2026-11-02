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

// ItemMessage returns an item as the contract states it.
func ItemMessage(i core.Item) *pb.Item { return itemOut(i) }

// ReceiptMessage returns a receipt, with the sequence of the event that holds it.
func ReceiptMessage(r core.Receipt, sequence uint64) *pb.Receipt { return receiptOut(r, sequence) }

// EventMessage returns an event as it is stored, without the bytes.
func EventMessage(sequence uint64, e core.Event) (*pb.Event, error) {
	data, err := EncodeEvent(sequence, e)
	if err != nil {
		return nil, err
	}
	var m pb.Event
	if err := proto.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("event %d: %w", sequence, err)
	}
	return &m, nil
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

// SidecarCommandFor returns what the sidecar is sent for an effect. It reports false for the
// effects that are not commands to a live session: launching a session and arming a timer.
func SidecarCommandFor(e core.Effect) (*pb.SidecarCommand, bool, error) {
	data, err := EncodeEffect(e)
	if err != nil {
		return nil, false, err
	}
	var m pb.Effect
	if err := proto.Unmarshal(data, &m); err != nil {
		return nil, false, fmt.Errorf("effect %s: %w", e.ID(), err)
	}
	out := &pb.SidecarCommand{EffectId: m.GetId()}
	switch b := m.GetBody().(type) {
	case *pb.Effect_StartTurn:
		out.Body = &pb.SidecarCommand_StartTurn{StartTurn: b.StartTurn}
	case *pb.Effect_Steer:
		out.Body = &pb.SidecarCommand_Steer{Steer: b.Steer}
	case *pb.Effect_Interrupt:
		out.Body = &pb.SidecarCommand_Interrupt{Interrupt: b.Interrupt}
	case *pb.Effect_CloseSession:
		out.Body = &pb.SidecarCommand_Close{Close: b.CloseSession}
	default:
		return nil, false, nil
	}
	return out, true, nil
}
