package wire_test

import (
	"math/rand/v2"
	"reflect"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// seedCorpus starts a fuzz target from valid messages of each body, and from some bytes that are not.
func seedCorpus(f *testing.F, make func(*rand.Rand) proto.Message) {
	f.Helper()
	r := rand.New(rand.NewPCG(11, 12))
	for range 64 {
		f.Add(encoded(make(r)))
	}
	f.Add([]byte{})
	f.Add([]byte{0xff, 0xff, 0xff, 0xff})
}

// world is a state with a live lead and subagent, for accepted input to land on.
func world(t *testing.T) (core.State, core.Session) {
	t.Helper()
	at := epoch
	step := func(s core.State, n int, body core.InputBody) core.State {
		s, _, _ = core.Decide(s, core.Input{At: at.Add(time.Duration(n) * time.Second), Nonce: core.Nonce(rune('a' + n)), Body: body})
		return s
	}
	s := step(core.State{}, 0, core.Command{ID: "start", Digest: "d", Caller: core.Operator{}, Body: core.StartRun{
		Repository: "r", Task: "t", FanoutLimit: 2, Brief: "go", Lead: core.AgentSpec{Harness: core.ClaudeCode, Model: "m"},
	}})
	run := s.Runs()[0]
	session := core.Session{Agent: run.Lead, N: 1}
	s = step(s, 1, core.SidecarFact{Session: session, Offset: 1, Body: core.ConversationOpened{
		ConversationID: "c", Capabilities: core.Capabilities{Steers: true, Resumes: true}, Worktree: "/w",
	}})
	return s, session
}

// Whatever a client sends, accepted input is something the core can decide on.
func FuzzParseCommand(f *testing.F) {
	seedCorpus(f, func(r *rand.Rand) proto.Message { return generated(r, &pb.Command{}) })
	f.Fuzz(func(t *testing.T, data []byte) {
		c, err := wire.ParseCommand(data, core.Operator{})
		if err != nil {
			return
		}
		s, _ := world(t)
		for _, state := range []core.State{{}, s} {
			next, _, _ := core.Decide(state, core.Input{At: epoch, Nonce: "n", Body: c})
			if v := next.Violations(); len(v) > 0 {
				t.Fatalf("%v", v)
			}
		}
	})
}

// Whatever a sidecar says, accepted input is something the core can decide on.
func FuzzParseSidecarEvent(f *testing.F) {
	seedCorpus(f, func(r *rand.Rand) proto.Message { return generated(r, &pb.SidecarEvent{}) })
	f.Fuzz(func(t *testing.T, data []byte) {
		s, from := world(t)
		fact, _, err := wire.ParseSidecarEvent(from, data)
		if err != nil || fact.Body == nil {
			return
		}
		next, _, _ := core.Decide(s, core.Input{At: epoch, Nonce: "n", Body: fact})
		if v := next.Violations(); len(v) > 0 {
			t.Fatalf("%v", v)
		}
	})
}

// A stored event decodes to one value however often it is read and rewritten, and damaged bytes are
// an error and never a panic.
func FuzzDecodeEvent(f *testing.F) {
	seedCorpus(f, func(r *rand.Rand) proto.Message { return generated(r, &pb.Event{}) })
	f.Fuzz(func(t *testing.T, data []byte) {
		seq, event, err := wire.DecodeEvent(data)
		if err != nil {
			return
		}
		again, err := wire.EncodeEvent(seq, event)
		if err != nil {
			t.Fatalf("an accepted event does not encode: %v", err)
		}
		seq2, event2, err := wire.DecodeEvent(again)
		if err != nil || seq2 != seq || !reflect.DeepEqual(event, event2) {
			t.Fatalf("rewriting changed an event: %+v, then %+v, err %v", event, event2, err)
		}
	})
}

// A stored effect decodes to one value however often it is read and rewritten.
func FuzzDecodeEffect(f *testing.F) {
	seedCorpus(f, func(r *rand.Rand) proto.Message { return generatedEffect(r) })
	f.Fuzz(func(t *testing.T, data []byte) {
		effect, err := wire.DecodeEffect(data)
		if err != nil {
			return
		}
		again, err := wire.EncodeEffect(effect)
		if err != nil {
			t.Fatalf("an accepted effect does not encode: %v", err)
		}
		effect2, err := wire.DecodeEffect(again)
		if err != nil || !reflect.DeepEqual(effect, effect2) {
			t.Fatalf("rewriting changed an effect: %+v, then %+v, err %v", effect, effect2, err)
		}
	})
}
