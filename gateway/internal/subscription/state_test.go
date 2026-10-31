package subscription

// test-isolation: these properties drive the pure core directly, because the
// interleavings of writes, slow reads, joins and leaves cannot be scheduled
// through goroutines. The public behavior is held by the contract suite.

import (
	"testing"
	"testing/quick"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	modelInstance = "instance"
	modelKeys     = 4
)

var modelPolicy = hostoperations.TopicPolicy{MaxKeys: modelKeys, MaxValueBytes: 1 << 20}

// action is one generated step. Kind picks the transition, and Who and Key pick
// its target, so every byte string is a valid schedule.
type action struct {
	Kind, Who, Key, Count uint8
}

// client folds frames as a real subscriber would, and checks what it can at
// each frame: a value is never older than one it already saw for that key.
type client struct {
	id     subscriberID
	values map[string]int64
	newest map[string]int64
	synced bool
	failed bool
}

type model struct {
	t       *testing.T
	state   *state
	at      generation
	written int64
	clients []*client
	running bool
}

func entry(key string, value int64) *apiv1.TopicEntry {
	return &apiv1.TopicEntry{Key: key, Value: &apiv1.TopicEntry_MachineStats{
		MachineStats: &apiv1.MachineStats{SampledAt: &timestamppb.Timestamp{Seconds: value}},
	}}
}

func (m *model) apply(work effects) {
	if work.stopped {
		if !m.running {
			m.t.Fatal("stopped a producer that was not running")
		}
		m.running = false
	}
}

func (m *model) join() {
	id, at, started, refusal := m.state.join(uint64(len(m.clients)%3), modelInstance, modelPolicy)
	if refusal != nil {
		return
	}
	if started {
		if m.running {
			m.t.Fatal("started a second producer for one instance")
		}
		m.running, m.at = true, at
	}
	m.clients = append(m.clients, &client{id: id, values: map[string]int64{}, newest: map[string]int64{}})
}

func (m *model) read(each *client, frames int) {
	for range frames {
		frame, terminal := m.state.next(each.id)
		if frame == nil {
			return
		}
		switch body := frame.Frame.(type) {
		case *apiv1.SubscribeResponse_Entry:
			key, value := body.Entry.Key, body.Entry.GetMachineStats().SampledAt.Seconds
			if value <= each.newest[key] {
				m.t.Fatalf("key %s went from value %d to %d", key, each.newest[key], value)
			}
			each.values[key], each.newest[key] = value, value
		case *apiv1.SubscribeResponse_Removed:
			if _, held := each.values[body.Removed]; !held {
				m.t.Fatalf("removal of key %s the client never held", body.Removed)
			}
			delete(each.values, body.Removed)
		case *apiv1.SubscribeResponse_Synced:
			if each.synced {
				m.t.Fatal("synced twice on one stream")
			}
			each.synced = true
			m.requireSameKeys(each)
		case *apiv1.SubscribeResponse_Failed:
			if !terminal {
				m.t.Fatal("failed frame was not terminal")
			}
			each.failed = true
			return
		}
	}
}

func (m *model) requireSameKeys(each *client) {
	values := m.state.subscribers[each.id].instance.values
	if len(values) != len(each.values) {
		m.t.Fatalf("synced with %d keys of %d", len(each.values), len(values))
	}
	for key := range values {
		if _, held := each.values[key]; !held {
			m.t.Fatalf("synced without key %s", key)
		}
	}
}

// requireBounded holds the conflation promise: bookkeeping is O(keys), however
// far behind a reader falls.
func (m *model) requireBounded() {
	for _, each := range m.state.subscribers {
		if len(each.delivered) > modelKeys || len(each.unsent) > modelKeys || len(each.changed) > modelKeys || len(each.removed) > modelKeys {
			m.t.Fatalf("bookkeeping exceeds %d keys: %d delivered, %d unsent, %d changed, %d removed",
				modelKeys, len(each.delivered), len(each.unsent), len(each.changed), len(each.removed))
		}
	}
	if _, live := m.state.instances[modelInstance]; live != m.running {
		m.t.Fatalf("instance live=%t while producer running=%t", live, m.running)
	}
	subscribers := len(m.state.subscribers)
	for _, each := range m.state.subscribers {
		if each.failure != nil {
			subscribers--
		}
	}
	if (subscribers > 0) != m.running {
		m.t.Fatalf("%d subscribers while producer running=%t", subscribers, m.running)
	}
}

func (m *model) step(next action) {
	key := string(rune('a' + next.Key%(modelKeys+1)))
	var who *client
	if len(m.clients) > 0 {
		who = m.clients[int(next.Who)%len(m.clients)]
	}
	switch next.Kind % 7 {
	case 0:
		m.join()
	case 1:
		if who != nil {
			m.read(who, int(next.Count%8))
		}
	case 2:
		if who != nil {
			m.apply(m.state.leave(who.id))
			m.clients = removeClient(m.clients, who)
		}
	case 3:
		m.written++
		m.apply(m.state.put(modelInstance, m.at, key, entry(key, m.written), 0))
	case 4:
		m.apply(m.state.remove(modelInstance, m.at, key))
	case 5:
		m.apply(m.state.synced(modelInstance, m.at))
	case 6:
		// A write from a producer already replaced must change nothing.
		m.written++
		m.apply(m.state.put(modelInstance, m.at-1, key, entry(key, m.written), 0))
	}
	m.requireBounded()
}

func removeClient(clients []*client, gone *client) []*client {
	kept := make([]*client, 0, len(clients))
	for _, each := range clients {
		if each != gone {
			kept = append(kept, each)
		}
	}
	return kept
}

func newModel(t *testing.T) *model {
	return &model{t: t, state: newState(limits{perConnection: 4, total: 8})}
}

func TestDeliveredStateConvergesToTheProducersUnderAnyInterleaving(t *testing.T) {
	property := func(actions []action) bool {
		m := newModel(t)
		for _, next := range actions {
			m.step(next)
		}
		for _, each := range m.clients {
			m.read(each, 4*modelKeys+4)
			if each.failed {
				continue
			}
			held := m.state.subscribers[each.id]
			if len(held.instance.values) != len(each.values) {
				return false
			}
			for key, stored := range held.instance.values {
				if each.values[key] != stored.GetMachineStats().SampledAt.Seconds {
					return false
				}
			}
			if each.synced != held.instance.synced {
				return false
			}
		}
		return true
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 2000}); err != nil {
		t.Fatal(err)
	}
}

func TestWritesOutrunningAReaderCostOneValuePerKey(t *testing.T) {
	property := func(writes []uint8) bool {
		m := newModel(t)
		m.join()
		for _, key := range writes {
			m.step(action{Kind: 3, Key: key % modelKeys})
		}
		sent := 0
		for {
			frame, _ := m.state.next(m.clients[0].id)
			if frame == nil {
				break
			}
			sent++
		}
		return sent <= modelKeys
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

// A fast producer always leaves a key changed. synced waits only for the first
// copy of every key, or a subscriber to a busy topic would never go live.
func TestSyncedIsNotStarvedByValueChurn(t *testing.T) {
	m := newModel(t)
	m.join()
	for key := range modelKeys {
		m.step(action{Kind: 3, Key: uint8(key)})
	}
	m.step(action{Kind: 5})
	reader := m.clients[0]
	for range modelKeys + 1 {
		m.read(reader, 1)
		m.step(action{Kind: 3, Key: 0})
	}
	if !reader.synced {
		t.Fatal("churn on a held key postponed synced")
	}
}

func TestCapsRefuseWithoutChangingAnything(t *testing.T) {
	s := newState(limits{perConnection: 2, total: 3})
	join := func(connection uint64) *apiv1.TopicFailure {
		_, _, _, refusal := s.join(connection, modelInstance, modelPolicy)
		return refusal
	}
	for _, connection := range []uint64{1, 1, 2} {
		if refusal := join(connection); refusal != nil {
			t.Fatal(refusal)
		}
	}
	for _, connection := range []uint64{1, 3} {
		refusal := join(connection)
		if refusal.GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED {
			t.Fatalf("connection %d: refusal %v", connection, refusal)
		}
	}
	if len(s.subscribers) != 3 || s.connections[3] != 0 {
		t.Fatalf("a refusal changed state: %d subscribers, connections %v", len(s.subscribers), s.connections)
	}
}

func TestAFailedInstanceIsReplacedAndItsProducerFenced(t *testing.T) {
	m := newModel(t)
	m.join()
	old := m.at
	m.apply(m.state.fail(modelInstance, old, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED, "gone")))
	m.join()
	if m.at == old {
		t.Fatal("a failed instance kept its generation")
	}
	key := "a"
	m.apply(m.state.put(modelInstance, old, key, entry(key, 1), 0))
	if frame, _ := m.state.next(m.clients[1].id); frame != nil {
		t.Fatalf("a stale producer reached the new instance: %v", frame)
	}
	frame, terminal := m.state.next(m.clients[0].id)
	if !terminal || frame.GetFailed() == nil {
		t.Fatalf("old subscriber got %v", frame)
	}
}
