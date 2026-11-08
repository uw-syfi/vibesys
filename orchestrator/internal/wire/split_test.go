package wire_test

import (
	"fmt"
	"math/rand/v2"
	"testing"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// publicPart is what a client may know of a stored event, written out from the record by hand. The
// mappers are not used, so it differs from what they give when they get one wrong.
func publicPart(in *pb.Event) *pb.RunChange {
	out := &pb.RunChange{Sequence: in.GetSequence(), At: in.GetAt(), RunId: in.GetRunId()}
	switch b := in.GetBody().(type) {
	case *pb.Event_RunUpdated:
		out.Body = &pb.RunChange_Run{Run: b.RunUpdated.GetRun()}
	case *pb.Event_AgentUpdated:
		out.Body = &pb.RunChange_Agent{Agent: b.AgentUpdated.GetAgent()}
	case *pb.Event_TurnUpdated:
		out.Body = &pb.RunChange_Turn{Turn: b.TurnUpdated}
	case *pb.Event_ItemUpdated:
		out.Body = &pb.RunChange_Item{Item: b.ItemUpdated}
	case *pb.Event_MessageUpdated:
		out.Body = &pb.RunChange_Message{Message: b.MessageUpdated}
	case *pb.Event_ReceiptRecorded:
		out.Body = &pb.RunChange_Receipt{Receipt: b.ReceiptRecorded.GetReceipt()}
	case *pb.Event_CheckInUpdated:
		out.Body = &pb.RunChange_CheckIn{CheckIn: b.CheckInUpdated}
	case *pb.Event_RunSettingsUpdated:
		out.Body = &pb.RunChange_RunUpdated{RunUpdated: b.RunSettingsUpdated}
	}
	return out
}

// storedEvent is a random event that a store could have written: its receipt has the event's
// sequence.
func storedEvent(r *rand.Rand) *pb.Event {
	in := generated(r, &pb.Event{})
	if receipt := in.GetReceiptRecorded().GetReceipt(); receipt != nil {
		receipt.Sequence = in.GetSequence()
	}
	if check := in.GetCheckInUpdated(); check != nil {
		legalCheckIn(r, check)
	}
	return in
}

// A watcher gets the public part of a record and nothing else. RunChange's own type cannot hold a
// private fact (archcheck holds that), so what this adds is that every public field gets there, the
// value of a private one cannot be smuggled through a public one, and each read of the core state a
// client is served says the same.
func TestOnlyThePublicPartOfARecordReachesAWatcher(t *testing.T) {
	r := rand.New(rand.NewPCG(21, 22))
	accepted := map[string]int{}
	for range rounds {
		in := storedEvent(r)
		seq, event, err := wire.DecodeEvent(encoded(in))
		if err != nil {
			continue
		}
		accepted[whichBody(in)]++
		got, err := wire.ChangeMessage(seq, event)
		if err != nil {
			t.Fatalf("change of %v: %v", in, err)
		}
		want := publicPart(in)
		if !proto.Equal(got, want) {
			t.Fatalf("a watcher was given more or less than the public part\n got: %v\nwant: %v", got, want)
		}
		if err := wire.Validate(got); err != nil {
			t.Fatalf("a change breaks its own contract: %v\n%v", err, got)
		}
		var served proto.Message
		var public proto.Message
		switch b := event.Body.(type) {
		case core.Run:
			served, public = wire.RunMessage(b), want.GetRun()
		case core.Agent:
			served, public = wire.AgentMessage(b), want.GetAgent()
		case core.Receipt:
			served, public = wire.ReceiptMessage(b, seq), want.GetReceipt()
		case core.CheckIn:
			served, public = wire.CheckInMessage(b), want.GetCheckIn()
		}
		if served != nil && !proto.Equal(served, public) {
			t.Fatalf("a snapshot or an answer gives more or less than the public part\n got: %v\nwant: %v", served, public)
		}
	}
	requireAll(t, &pb.Event{}, accepted)
}

// folded is a client's picture of the entities a stream has shown, by kind and id.
type folded map[string]proto.Message

func (f folded) apply(c *pb.RunChange) {
	switch b := c.GetBody().(type) {
	case *pb.RunChange_Run:
		f["run/"+b.Run.GetId()] = b.Run
	case *pb.RunChange_Agent:
		f["agent/"+b.Agent.GetId()] = b.Agent
	case *pb.RunChange_Turn:
		f["turn/"+b.Turn.GetId()] = b.Turn
	case *pb.RunChange_Item:
		f["item/"+b.Item.GetId()] = b.Item
	case *pb.RunChange_Message:
		f["message/"+b.Message.GetId()] = b.Message
	}
}

// snapshotOf is what a snapshot says of each entity: its message, from the core value of its latest
// event, by the reads the service serves a snapshot with.
func (f folded) snapshotOf(e core.Event) {
	switch b := e.Body.(type) {
	case core.Run:
		f["run/"+string(b.ID)] = wire.RunMessage(b)
	case core.Agent:
		f["agent/"+string(b.ID)] = wire.AgentMessage(b)
	case core.Turn:
		f["turn/"+string(b.ID)] = wire.TurnMessage(b)
	case core.Item:
		f["item/"+string(b.ID)] = wire.ItemMessage(b)
	case core.Message:
		f["message/"+string(b.ID)] = wire.MailboxMessage(b)
	}
}

// withFewIDs gives an event's entity an id from a small set, so that later events replace earlier
// ones as a run's updates do.
func withFewIDs(r *rand.Rand, in *pb.Event) {
	id := fmt.Sprintf("id-%d", r.IntN(4))
	switch b := in.GetBody().(type) {
	case *pb.Event_RunUpdated:
		if run := b.RunUpdated.GetRun(); run != nil {
			run.Id = id
		}
	case *pb.Event_AgentUpdated:
		if agent := b.AgentUpdated.GetAgent(); agent != nil {
			agent.Id = id
		}
	case *pb.Event_TurnUpdated:
		b.TurnUpdated.Id = id
	case *pb.Event_ItemUpdated:
		b.ItemUpdated.Id = id
	case *pb.Event_MessageUpdated:
		b.MessageUpdated.Id = id
	}
}

// Whatever a log holds, a fold of the changes it sends a watcher and a snapshot taken at the end say
// the same of every entity.
func TestAFoldOfChangesEqualsTheSnapshot(t *testing.T) {
	r := rand.New(rand.NewPCG(23, 24))
	for trial := range 100 {
		fold, snapshot := folded{}, folded{}
		for seq := uint64(1); seq <= 60; seq++ {
			in := storedEvent(r)
			in.Sequence = seq
			if receipt := in.GetReceiptRecorded().GetReceipt(); receipt != nil {
				receipt.Sequence = seq
			}
			withFewIDs(r, in)
			_, event, err := wire.DecodeEvent(encoded(in))
			if err != nil {
				continue
			}
			change, err := wire.ChangeMessage(seq, event)
			if err != nil {
				t.Fatal(err)
			}
			fold.apply(change)
			snapshot.snapshotOf(event)
		}
		if len(fold) == 0 || len(fold) != len(snapshot) {
			t.Fatalf("trial %d: the fold holds %d entities and the snapshot %d", trial, len(fold), len(snapshot))
		}
		for key, want := range snapshot {
			if got := fold[key]; !proto.Equal(got, want) {
				t.Fatalf("trial %d: %s folds to %v, the snapshot says %v", trial, key, got, want)
			}
		}
	}
}
