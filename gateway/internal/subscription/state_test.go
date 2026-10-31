package subscription

// test-isolation: these properties drive the pure core directly, because the
// interleavings of writes, slow reads, joins and leaves cannot be scheduled
// through goroutines. The public behavior is held by the contract suite.

import (
	"cmp"
	"slices"
	"testing"
	"testing/quick"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	modelTopic    = "machine_stats"
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

// model is the shell the core is run against. It holds every producer the core
// asked for until the model chooses to end it, which is how it stands in for a
// source that returns late or never.
type model struct {
	t       *testing.T
	state   *state
	at      generation
	written int64
	clients []*client
	alive   map[generation]*producerRun
}

// producerRun is a started producer: asked to stop, and whether its stop timer
// is armed (the shell arms one with every stop request).
type producerRun struct {
	ref      producerRef
	stopping bool
}

func entry(key string, value int64) *apiv1.TopicEntry {
	return &apiv1.TopicEntry{Key: key, Value: &apiv1.TopicEntry_MachineStats{
		MachineStats: &apiv1.MachineStats{SampledAt: &timestamppb.Timestamp{Seconds: value}},
	}}
}

func (m *model) apply(work effects) {
	if work.stop != nil {
		run := m.alive[work.stop.at]
		if run == nil {
			m.t.Fatalf("stopped producer %d that is not alive", work.stop.at)
		}
		if run.stopping {
			m.t.Fatalf("stopped producer %d twice", work.stop.at)
		}
		run.stopping = true
	}
	if work.start != nil {
		if len(m.alive) != 0 {
			m.t.Fatalf("started producer %d with %d alive: two producers of one instance overlap", work.start.at, len(m.alive))
		}
		m.alive[work.start.at] = &producerRun{ref: *work.start}
		m.at = work.start.at
	}
}

func (m *model) join() {
	id, work, refusal := m.state.join(uint64(len(m.clients)%3), modelTopic, modelInstance, modelPolicy)
	if refusal != nil {
		if refusal.GetCode() == apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED && !m.stuck() {
			m.t.Fatalf("refused for a stuck producer with none stuck: %v", refusal)
		}
		return
	}
	m.apply(work)
	m.clients = append(m.clients, &client{id: id, values: map[string]int64{}, newest: map[string]int64{}})
}

// stuck reports whether a producer is on record as unable to stop.
func (m *model) stuck() bool {
	for _, life := range m.state.producers {
		if life.abandoned {
			return true
		}
	}
	return false
}

// pick chooses an alive producer, the stopping ones only if asked.
func (m *model) pick(who uint8, stopping bool) *producerRun {
	var runs []*producerRun
	for _, run := range m.alive {
		if !stopping || run.stopping {
			runs = append(runs, run)
		}
	}
	if len(runs) == 0 {
		return nil
	}
	slices.SortFunc(runs, func(a, b *producerRun) int { return cmp.Compare(a.ref.at, b.ref.at) })
	return runs[int(who)%len(runs)]
}

// end returns a producer, which for a producer nobody asked to stop is the
// source ending on its own.
func (m *model) end(run *producerRun) {
	delete(m.alive, run.ref.at)
	m.apply(m.state.ended(run.ref, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED, "source stopped")))
}

// settle returns every producer that was asked to stop, as a well-behaved
// source does, until none remains. A producer that ends may start the instance
// that waited on it, which nobody has asked to stop.
func (m *model) settle() {
	for run := m.pick(0, true); run != nil; run = m.pick(0, true) {
		m.end(run)
	}
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

// requireBounded holds the conflation promise (bookkeeping is O(keys), however
// far behind a reader falls) and the producer lifecycle: the core's record of
// producers is the shell's, at most one runs for the key, an instance has one
// or is waiting on one that is bounded by a timer, and a producer that will
// not stop is never overlapped.
func (m *model) requireBounded() {
	for _, each := range m.state.subscribers {
		if len(each.delivered) > modelKeys || len(each.unsent) > modelKeys || len(each.changed) > modelKeys || len(each.removed) > modelKeys {
			m.t.Fatalf("bookkeeping exceeds %d keys: %d delivered, %d unsent, %d changed, %d removed",
				modelKeys, len(each.delivered), len(each.unsent), len(each.changed), len(each.removed))
		}
	}
	if len(m.state.producers) != len(m.alive) {
		m.t.Fatalf("core records %d producers, %d are alive", len(m.state.producers), len(m.alive))
	}
	if len(m.alive) > 2 {
		m.t.Fatalf("%d producers alive for one instance: a stopping one and its successor at most", len(m.alive))
	}
	running := 0
	for _, run := range m.alive {
		if !run.stopping {
			running++
		}
	}
	current, live := m.state.instances[modelInstance]
	switch {
	case !live && running != 0:
		m.t.Fatal("a producer runs with no instance")
	case live && !current.waiting:
		if m.alive[current.generation] == nil || m.alive[current.generation].stopping {
			m.t.Fatalf("instance %d has no running producer", current.generation)
		}
	case live && current.waiting:
		// No orphan waits: the producer it waits on has a stop timer, and is not
		// one the core has already given up on.
		if running != 0 || len(m.alive) != 1 || m.stuck() {
			m.t.Fatalf("instance waits on %d alive producers, %d running, stuck=%t", len(m.alive), running, m.stuck())
		}
	}
	subscribers := len(m.state.subscribers)
	for _, each := range m.state.subscribers {
		if each.failure != nil {
			subscribers--
		}
	}
	if (subscribers > 0) != live {
		m.t.Fatalf("%d subscribers while instance live=%t", subscribers, live)
	}
}

func (m *model) step(next action) {
	key := string(rune('a' + next.Key%(modelKeys+1)))
	var who *client
	if len(m.clients) > 0 {
		who = m.clients[int(next.Who)%len(m.clients)]
	}
	switch next.Kind % 9 {
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
	case 7:
		// A producer returns, late if it was asked to stop, and unasked if not.
		if run := m.pick(next.Who, false); run != nil {
			m.end(run)
		}
	case 8:
		// A stop's grace runs out.
		if run := m.pick(next.Who, true); run != nil {
			m.apply(m.state.overdue(run.ref))
		}
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
	return &model{t: t, state: newState(limits{perConnection: 4, total: 8}), alive: map[generation]*producerRun{}}
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

// What a flush sends is fixed when it starts, and bounded by the key bound however
// the writes and reads interleaved before it: the hub's interval and heartbeat
// both rely on a flush ending.
func TestAFlushSendsExactlyWhatWasOwedAndNoMoreThanTheKeyBoundAllows(t *testing.T) {
	property := func(actions []action) bool {
		m := newModel(t)
		for _, next := range actions {
			m.step(next)
		}
		for _, each := range m.clients {
			owed := m.state.owed(each.id)
			if owed > 2*modelKeys+2 {
				return false
			}
			sent := 0
			for {
				frame, terminal := m.state.next(each.id)
				if frame == nil {
					break
				}
				sent++
				if terminal {
					break
				}
			}
			if failed := m.state.subscribers[each.id].failure != nil; failed && sent != 1 || !failed && sent != owed {
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
		_, _, refusal := s.join(connection, modelTopic, modelInstance, modelPolicy)
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
	if len(m.alive) != 1 || m.state.instances[modelInstance].waiting != true {
		t.Fatal("a new instance did not wait for the producer still stopping")
	}
	m.end(m.alive[old])
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

// However late or never producers return, every instance that waits on one is
// either started when it ends or failed when its grace runs out, and a producer
// that never returns is on record once and refuses new subscribers instead of
// costing one each.
func TestEveryWaitingInstanceStartsOrFailsAndAStuckProducerIsNeverOverlapped(t *testing.T) {
	property := func(actions []action) bool {
		m := newModel(t)
		for _, next := range actions {
			m.step(next)
		}
		// Every grace runs out, then every stopping producer returns.
		for run := m.pick(0, true); run != nil; run = m.pick(0, true) {
			if !m.state.producers[run.ref.key].abandoned {
				m.apply(m.state.overdue(run.ref))
			}
			if next := m.state.instances[modelInstance]; next != nil && next.waiting {
				t.Fatal("an instance is still waiting after its grace ran out")
			}
			m.end(run)
			m.requireBounded()
		}
		if next := m.state.instances[modelInstance]; next != nil && next.waiting {
			return false
		}
		return len(m.alive) <= 1
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 2000}); err != nil {
		t.Fatal(err)
	}
}
