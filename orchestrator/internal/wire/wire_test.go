package wire_test

import (
	"math/rand/v2"
	"testing"

	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

const rounds = 3000

var session = core.Session{Agent: "agent", N: 1}

// whichBody names the oneof a generated message chose, to prove each one was reached.
func whichBody(m proto.Message) string {
	r := m.ProtoReflect()
	oneof := r.Descriptor().Oneofs().ByName("body")
	if fd := r.WhichOneof(oneof); fd != nil {
		return string(fd.Name())
	}
	return ""
}

func allBodies(m proto.Message) []string {
	fields := m.ProtoReflect().Descriptor().Oneofs().ByName("body").Fields()
	out := make([]string, 0, fields.Len())
	for i := range fields.Len() {
		out = append(out, string(fields.Get(i).Name()))
	}
	return out
}

func requireAll(t *testing.T, m proto.Message, accepted map[string]int) {
	t.Helper()
	for _, name := range allBodies(m) {
		if accepted[name] == 0 {
			t.Errorf("no generated %s with body %s was accepted", m.ProtoReflect().Descriptor().Name(), name)
		}
	}
}

// Every field of every message the core stores comes back from a trip through the core types, and
// every enum value has a name there.
func TestAnEventSurvivesTheCoreTypes(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	accepted := map[string]int{}
	for range rounds {
		in := generated(r, &pb.Event{})
		if receipt := in.GetReceiptRecorded(); receipt != nil {
			receipt.Sequence = in.GetSequence()
		}
		seq, event, err := wire.DecodeEvent(encoded(in))
		if err != nil {
			continue
		}
		accepted[whichBody(in)]++
		data, err := wire.EncodeEvent(seq, event)
		if err != nil {
			t.Fatalf("encode %v: %v", in, err)
		}
		out := &pb.Event{}
		if err := proto.Unmarshal(data, out); err != nil {
			t.Fatal(err)
		}
		if !proto.Equal(in, out) {
			t.Fatalf("event changed in a round trip\n in: %v\nout: %v", in, out)
		}
	}
	requireAll(t, &pb.Event{}, accepted)
}

func TestAnEffectSurvivesTheCoreTypes(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 4))
	accepted := map[string]int{}
	for range rounds {
		in := generatedEffect(r)
		effect, err := wire.DecodeEffect(encoded(in))
		if err != nil {
			continue
		}
		accepted[whichBody(in)]++
		data, err := wire.EncodeEffect(effect)
		if err != nil {
			t.Fatalf("encode %v: %v", in, err)
		}
		out := &pb.Effect{}
		if err := proto.Unmarshal(data, out); err != nil {
			t.Fatal(err)
		}
		if !proto.Equal(in, out) {
			t.Fatalf("effect changed in a round trip\n in: %v\nout: %v", in, out)
		}
	}
	requireAll(t, &pb.Effect{}, accepted)
}

// generatedEffect is a random effect with the id its body gives it.
func generatedEffect(r *rand.Rand) *pb.Effect {
	in := generated(r, &pb.Effect{})
	ref := func(s *pb.SessionRef) core.Session {
		return core.Session{Agent: core.AgentID(s.GetAgentId()), N: s.GetSession()}
	}
	var e core.Effect
	switch b := in.GetBody().(type) {
	case *pb.Effect_LaunchSession:
		e = core.LaunchSession{Session: ref(b.LaunchSession.GetSession())}
	case *pb.Effect_StartTurn:
		e = core.StartTurn{Turn: core.TurnID(b.StartTurn.GetTurnId())}
	case *pb.Effect_Steer:
		e = core.Steer{Message: core.Message{ID: core.MessageID(b.Steer.GetMessage().GetId())}}
	case *pb.Effect_Interrupt:
		e = core.Interrupt{Turn: core.TurnID(b.Interrupt.GetTurnId())}
	case *pb.Effect_CloseSession:
		e = core.CloseSession{Session: ref(b.CloseSession.GetSession()), Force: b.CloseSession.GetForce()}
	case *pb.Effect_ArmTimer:
		e = armed(b.ArmTimer, ref(b.ArmTimer.GetSession()))
	case *pb.Effect_ReleaseCheckouts:
		e = core.ReleaseCheckouts{Run: core.RunID(b.ReleaseCheckouts.GetRunId())}
	}
	in.Id = string(e.ID())
	return in
}

// armed is the timer's core value, with a deadline's session dropped because a deadline has none.
func armed(t *pb.ArmTimer, session core.Session) core.ArmTimer {
	switch t.GetKind() {
	case pb.TimerKind_TIMER_KIND_RUN_DEADLINE:
		t.Session = nil
		return core.ArmTimer{Kind: core.TimerRunDeadline, Run: core.RunID(t.GetRunId())}
	case pb.TimerKind_TIMER_KIND_LEAD_IDLE:
		t.Session = nil
		return core.ArmTimer{Kind: core.TimerLeadIdle, Run: core.RunID(t.GetRunId()), FireAt: t.GetFireAt().AsTime()}
	}
	return core.ArmTimer{Kind: core.TimerStopGrace, Session: session}
}

// A command's digest tells a retry from a different command under the same id, so it follows the
// content and not the bytes' origin.
func TestACommandDigestFollowsItsContent(t *testing.T) {
	r := rand.New(rand.NewPCG(5, 6))
	accepted := map[string]int{}
	for range rounds {
		in := generated(r, &pb.Command{})
		first, err := wire.ParseCommand(encoded(in), core.Operator{})
		if err != nil {
			continue
		}
		accepted[whichBody(in)]++
		again, err := wire.ParseCommand(encoded(in), core.AgentCaller{Agent: "a"})
		if err != nil {
			t.Fatal(err)
		}
		if first.Digest == "" || first.Digest != again.Digest {
			t.Fatalf("digests %q and %q of one command", first.Digest, again.Digest)
		}
		changed := proto.CloneOf(in)
		changed.Id = in.Id + "-other"
		if other, err := wire.ParseCommand(encoded(changed), core.Operator{}); err != nil || other.Digest != first.Digest {
			t.Fatalf("the id is not content: digest %q, want %q, err %v", other.Digest, first.Digest, err)
		}
	}
	requireAll(t, &pb.Command{}, accepted)
}

func TestSidecarEventsParse(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 8))
	accepted := map[string]int{}
	for range rounds {
		in := generated(r, &pb.SessionEvent{})
		fact, delta, err := wire.ParseSessionEvent(session, encoded(in))
		if err != nil {
			continue
		}
		accepted[whichBody(in)]++
		if _, isDelta := in.GetBody().(*pb.SessionEvent_Delta); isDelta != (delta != nil) || isDelta != (fact.Body == nil) {
			t.Fatalf("%v parsed to fact %+v and delta %v", in, fact, delta)
		}
		if !isDeltaOf(in) && (fact.Session != session || fact.Offset != in.GetOffset()) {
			t.Fatalf("%v parsed to %+v", in, fact)
		}
	}
	requireAll(t, &pb.SessionEvent{}, accepted)
}

func isDeltaOf(in *pb.SessionEvent) bool {
	_, ok := in.GetBody().(*pb.SessionEvent_Delta)
	return ok
}

// withUnknown adds a field this build does not know to a message.
func withUnknown(m protoreflect.Message) {
	m.SetUnknown(protowire.AppendVarint(protowire.AppendTag(nil, 9999, protowire.VarintType), 1))
}

// Unknown fields are refused at any depth. Silently dropping one would mean a newer client's intent
// is lost without a word.
func TestAnUnknownFieldIsRefusedAtAnyDepth(t *testing.T) {
	parsers := []struct {
		name  string
		make  func(*rand.Rand) proto.Message
		parse func([]byte) error
	}{
		{"event", func(r *rand.Rand) proto.Message { return generated(r, &pb.Event{}) }, func(b []byte) error {
			_, _, err := wire.DecodeEvent(b)
			return err
		}},
		{"effect", func(r *rand.Rand) proto.Message { return generatedEffect(r) }, func(b []byte) error {
			_, err := wire.DecodeEffect(b)
			return err
		}},
		{"command", func(r *rand.Rand) proto.Message { return generated(r, &pb.Command{}) }, func(b []byte) error {
			_, err := wire.ParseCommand(b, core.Operator{})
			return err
		}},
		{"sidecar event", func(r *rand.Rand) proto.Message { return generated(r, &pb.SessionEvent{}) }, func(b []byte) error {
			_, _, err := wire.ParseSessionEvent(session, b)
			return err
		}},
	}
	for _, p := range parsers {
		t.Run(p.name, func(t *testing.T) {
			r := rand.New(rand.NewPCG(9, 10))
			refused := 0
			for range rounds / 10 {
				m := p.make(r)
				if p.parse(encoded(m)) != nil {
					continue
				}
				nested := nestedMessages(m.ProtoReflect())
				withUnknown(nested[r.IntN(len(nested))])
				if err := p.parse(encoded(m)); err == nil {
					t.Fatalf("an unknown field in %v was accepted", m)
				}
				refused++
			}
			if refused == 0 {
				t.Fatal("no message was accepted to begin with")
			}
		})
	}
}

// nestedMessages lists m and the messages inside it, except timestamps, which are not ours.
func nestedMessages(m protoreflect.Message) []protoreflect.Message {
	out := []protoreflect.Message{m}
	m.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		switch {
		case fd.Message() == nil || fd.Message().FullName() == "google.protobuf.Timestamp":
		case fd.IsList():
			for i := range v.List().Len() {
				out = append(out, nestedMessages(v.List().Get(i).Message())...)
			}
		default:
			out = append(out, nestedMessages(v.Message())...)
		}
		return true
	})
	return out
}

func TestAMessageMissingItsBodyIsRefused(t *testing.T) {
	if _, _, err := wire.DecodeEvent(encoded(&pb.Event{Sequence: 1})); err == nil {
		t.Error("an event with no body was accepted")
	}
	if _, err := wire.DecodeEffect(encoded(&pb.Effect{Id: "e"})); err == nil {
		t.Error("an effect with no body was accepted")
	}
	if _, err := wire.ParseCommand(encoded(&pb.Command{Id: "c"}), core.Operator{}); err == nil {
		t.Error("a command with no body was accepted")
	}
	if _, _, err := wire.ParseSessionEvent(session, encoded(&pb.SessionEvent{Offset: 1})); err == nil {
		t.Error("a sidecar event with no body was accepted")
	}
}
