package wire_test

import (
	"math/rand/v2"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// When an item was first seen is kept by the stored record and by what a client is given, and an item
// recorded before the host kept it has none: absent on the wire, never a zero time.
func TestWhenAnItemWasFirstSeenIsKeptOrAbsent(t *testing.T) {
	r := rand.New(rand.NewPCG(31, 32))
	at := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	for range 200 {
		item := core.Item{
			ID: "item-1", Turn: "turn-1", Agent: "agent-1", Ordinal: 1 + uint32(r.IntN(1000)),
			Status: core.ItemFinished, At: at.Add(time.Duration(r.IntN(1_000_000)) * time.Millisecond),
			Body: core.Assistant{Text: "hi"},
		}
		known := r.IntN(2) == 0
		if known {
			item.Started = item.At.Add(-time.Duration(r.IntN(1_000_000)) * time.Millisecond)
		}
		data, err := wire.EncodeEvent(7, core.Event{Run: "run-1", At: at, Body: item})
		if err != nil {
			t.Fatal(err)
		}
		_, event, err := wire.DecodeEvent(data)
		if err != nil {
			t.Fatal(err)
		}
		back, ok := event.Body.(core.Item)
		if !ok || !back.Started.Equal(item.Started) || back.Started.IsZero() != !known {
			t.Fatalf("started %v came back as %v (known %v)", item.Started, back.Started, known)
		}
		message := wire.ItemMessage(back)
		if (message.GetStartedAt() != nil) != known {
			t.Fatalf("known %v, but the wire says %v", known, message.GetStartedAt())
		}
		if known && !message.GetStartedAt().AsTime().Equal(item.Started) {
			t.Fatalf("the wire says %v, want %v", message.GetStartedAt().AsTime(), item.Started)
		}
		var stored pb.Event
		if err := proto.Unmarshal(data, &stored); err != nil {
			t.Fatal(err)
		}
		if (stored.GetItemUpdated().GetStartedAt() != nil) != known {
			t.Fatalf("the stored record says %v for known %v", stored.GetItemUpdated().GetStartedAt(), known)
		}
	}
}
