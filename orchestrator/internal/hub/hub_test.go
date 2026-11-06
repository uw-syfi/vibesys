package hub_test

import (
	"fmt"
	"math/rand/v2"
	"os"
	"strconv"
	"strings"
	"testing"

	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
)

func itemEvent(seq uint64, agent, item, text string, running bool) hub.Stored {
	status := pb.ItemStatus_ITEM_STATUS_FINISHED
	if running {
		status = pb.ItemStatus_ITEM_STATUS_RUNNING
	}
	return hub.Stored{
		Agent: core.AgentID(agent), Item: core.ItemID(item), ItemFinal: !running,
		Change: &pb.RunChange{Sequence: seq, At: timestamppb.Now(), Body: &pb.RunChange_Item{Item: &pb.Item{
			Id: item, AgentId: agent, Status: status, Body: &pb.Item_Assistant{Assistant: &pb.Text{Text: text}},
		}}},
	}
}

func textDelta(agent, item, text string) *pb.Delta {
	return &pb.Delta{AgentId: agent, ItemId: item, Body: &pb.Delta_Text{Text: text}}
}

func TestDeltasMergeWithinOneWindow(t *testing.T) {
	h := hub.New(hub.DefaultLimits)
	sub := h.Subscribe(hub.Interest{Agent: "a"})
	for _, part := range []string{"he", "llo", " world"} {
		h.PublishDelta(textDelta("a", "i1", part))
	}
	if got, _ := sub.Take(); len(got) != 0 {
		t.Fatalf("a delta was readable before the window ended: %v", got)
	}
	h.Flush()
	got, lagged := sub.Take()
	if lagged || len(got) != 1 || got[0].Delta.GetText() != "hello world" {
		t.Fatalf("got %v lagged=%v, want one merged delta", got, lagged)
	}
}

func TestAStoredVersionSupersedesItsPendingDeltas(t *testing.T) {
	h := hub.New(hub.DefaultLimits)
	sub := h.Subscribe(hub.Interest{Agent: "a"})
	h.PublishDelta(textDelta("a", "i1", "abc"))
	h.PublishStored(itemEvent(7, "a", "i1", "abc", true))
	h.Flush()
	got, _ := sub.Take()
	if len(got) != 1 || got[0].Stored == nil {
		t.Fatalf("got %v, want only the stored version, with no stale delta after it", got)
	}
}

func TestAFinalItemTakesNoLateDelta(t *testing.T) {
	h := hub.New(hub.DefaultLimits)
	sub := h.Subscribe(hub.Interest{Agent: "a"})
	h.PublishStored(itemEvent(1, "a", "i1", "done", false))
	h.PublishDelta(textDelta("a", "i1", "late"))
	h.Flush()
	if got, _ := sub.Take(); len(got) != 1 || got[0].Stored == nil {
		t.Fatalf("got %v, want only the stored version", got)
	}
}

func TestEventsOfOtherAgentsDoNotReachAWatcher(t *testing.T) {
	h := hub.New(hub.DefaultLimits)
	watcher, everyone := h.Subscribe(hub.Interest{Agent: "a"}), h.Subscribe(hub.Interest{Everything: true})
	h.PublishStored(itemEvent(1, "b", "i9", "x", false))
	h.PublishDelta(textDelta("b", "i9", "x"))
	h.Flush()
	if got, _ := watcher.Take(); len(got) != 0 {
		t.Fatalf("watcher of a received %v", got)
	}
	got, _ := everyone.Take()
	if len(got) != 1 || got[0].Stored == nil {
		t.Fatalf("watcher of everything got %v, want the stored event and no delta", got)
	}
}

func TestASlowClientLagsAndTheWriterIsNeverBlocked(t *testing.T) {
	h := hub.New(hub.Limits{MaxItems: 3, MaxBytes: 1 << 20})
	sub := h.Subscribe(hub.Interest{Everything: true})
	for seq := uint64(1); seq <= 10; seq++ {
		h.PublishStored(itemEvent(seq, "a", fmt.Sprint("i", seq), "x", false))
	}
	if _, lagged := sub.Take(); !lagged {
		t.Fatal("a client 7 events over its bound was not told to start again")
	}
	sub.Resume()
	h.PublishStored(itemEvent(11, "a", "i11", "x", false))
	got, lagged := sub.Take()
	if lagged || len(got) != 1 || got[0].Stored.Change.GetSequence() != 11 {
		t.Fatalf("after resuming got %v lagged=%v, want event 11 only", got, lagged)
	}
}

func TestTheByteBoundLagsToo(t *testing.T) {
	h := hub.New(hub.Limits{MaxItems: 100, MaxBytes: 300})
	sub := h.Subscribe(hub.Interest{Everything: true})
	h.PublishStored(itemEvent(1, "a", "i1", strings.Repeat("x", 400), false))
	if _, lagged := sub.Take(); !lagged {
		t.Fatal("an event over the byte bound did not lag the client")
	}
}

func TestClosedSubscriptionsCostNothing(t *testing.T) {
	h := hub.New(hub.DefaultLimits)
	sub := h.Subscribe(hub.Interest{Everything: true})
	sub.Close()
	if h.Subscribers() != 0 {
		t.Fatal("a closed subscription is still held")
	}
	h.PublishStored(itemEvent(1, "a", "i", "x", false))
}

// truth is what the writer knows: the latest stored text of each item, with the deltas since.
type truth map[string]string

// view is what a client reconstructs from what it was sent.
type view struct {
	items map[string]string
	seq   uint64
}

func (v *view) apply(m hub.Message) error {
	switch {
	case m.Stored != nil:
		seq := m.Stored.Change.GetSequence()
		if seq <= v.seq {
			return fmt.Errorf("stored event %d arrived after %d", seq, v.seq)
		}
		v.seq = seq
		it := m.Stored.Change.GetItem()
		v.items[it.GetId()] = it.GetAssistant().GetText()
	case m.Delta != nil:
		v.items[m.Delta.GetItemId()] += m.Delta.GetText()
	}
	return nil
}

func seeds() []uint64 {
	if v := os.Getenv("ORCHESTRATOR_HUB_SEED"); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			panic(err)
		}
		return []uint64{n}
	}
	out := make([]uint64, 300)
	for i := range out {
		out[i] = uint64(i + 1)
	}
	return out
}

// TestAClientNeverSeesStaleText runs random publishing against clients that read at random, some
// with tight bounds. Whatever it reads, an item's text is a prefix of the truth, equal to it for a
// client that never fell behind, and stored events come in order and are never skipped.
func TestAClientNeverSeesStaleText(t *testing.T) {
	for _, seed := range seeds() {
		if err := runHubSeed(seed); err != nil {
			t.Fatalf("seed %d: %v\nreplay: ORCHESTRATOR_HUB_SEED=%d go test ./internal/hub -run TestAClientNeverSeesStaleText", seed, err, seed)
		}
	}
}

func runHubSeed(seed uint64) error {
	rng := rand.New(rand.NewPCG(seed, 1))
	tight := rng.IntN(2) == 0
	limits := hub.DefaultLimits
	if tight {
		limits = hub.Limits{MaxItems: 2 + rng.IntN(8), MaxBytes: 200 + rng.IntN(800)}
	}
	h := hub.New(limits)
	sub := h.Subscribe(hub.Interest{Agent: "a"})
	real := truth{}
	client := &view{items: map[string]string{}}
	var seq uint64
	fellBehind := false
	read := func() error {
		msgs, lagged := sub.Take()
		if lagged {
			fellBehind = true
			client = &view{items: maps(real), seq: seq}
			sub.Resume()
			return nil
		}
		for _, m := range msgs {
			if err := client.apply(m); err != nil {
				return err
			}
		}
		return nil
	}
	for step := 0; step < 200; step++ {
		item := fmt.Sprint("i", rng.IntN(3))
		switch rng.IntN(6) {
		case 0, 1, 2:
			part := strings.Repeat("x", 1+rng.IntN(30))
			real[item] += part
			h.PublishDelta(textDelta("a", item, part))
		case 3:
			seq++
			final := rng.IntN(3) == 0
			if final {
				item = fmt.Sprint("i", rng.IntN(3), "-", seq)
			}
			h.PublishStored(itemEvent(seq, "a", item, real[item], !final))
			if final {
				delete(real, item)
			}
		case 4:
			seq++
			h.PublishStored(itemEvent(seq, "b", "other", "ignored", false))
		case 5:
			h.Flush()
			if err := read(); err != nil {
				return err
			}
		}
		for it, text := range client.items {
			if !strings.HasPrefix(real[it], text) {
				return fmt.Errorf("step %d: client holds %q for %s, truth is %q", step, text, it, real[it])
			}
		}
	}
	h.Flush()
	if err := read(); err != nil {
		return err
	}
	if !tight && !fellBehind {
		for it, text := range real {
			if client.items[it] != text {
				return fmt.Errorf("with room to spare the client holds %q for %s, truth is %q", client.items[it], it, text)
			}
		}
	}
	return nil
}

func maps(t truth) map[string]string {
	out := map[string]string{}
	for k, v := range t {
		out[k] = v
	}
	return out
}

// A subscription that lives as long as a client stays connected remembers a bounded number of
// finished items. The recent ones still refuse a late delta; one forgotten after that many others
// would show a stale delta, which is advisory and superseded by the stored item.
func TestAConnectionRemembersOnlyABoundedNumberOfFinishedItems(t *testing.T) {
	h := hub.New(hub.Limits{MaxItems: 1 << 30, MaxBytes: 1 << 30})
	sub := h.Subscribe(hub.Interest{Agent: "a"})
	total := hub.MaxRemembered + 10
	for i := range total {
		h.PublishStored(itemEvent(uint64(i+1), "a", fmt.Sprintf("i%d", i), "done", false))
	}
	sub.Take()
	h.PublishDelta(textDelta("a", fmt.Sprintf("i%d", total-1), "late"))
	h.PublishDelta(textDelta("a", "i0", "very late"))
	h.Flush()
	got, _ := sub.Take()
	if len(got) != 1 || got[0].Delta.GetItemId() != "i0" {
		t.Fatalf("got %v, want the late delta of the newest finished item refused and the forgotten oldest one shown", got)
	}
}
