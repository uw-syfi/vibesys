package subscription

import (
	"fmt"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/proto"
)

// limits caps open subscriptions. A breach is a typed refusal, never a queue.
type limits struct{ perConnection, total int }

type subscriberID uint64

// generation fences a stopped or failed producer: a late Put from one cannot
// touch the instance that replaced it, as lifecycle.Idle fences a late timer.
type generation uint64

// state is the hub's pure core: subscribers, the retained map of each topic
// instance, the producer goroutine alive for each, and what each subscriber
// still owes its client. It holds no goroutines, locks or clocks. Each
// transition returns what the shell must do, and the shell reports back every
// producer's end and every stop that ran past its grace.
//
// Memory is O(keys) per subscriber and retains one value per key: a slow
// subscriber costs key names, never a queue of values.
type state struct {
	limits      limits
	instances   map[string]*instance
	producers   map[string]*lifeline
	subscribers map[subscriberID]*subscriber
	connections map[uint64]int
	nextID      uint64
}

// instance is one topic instance. It has a producer from the moment one is
// started, which is at once unless an earlier producer of the key is still
// stopping: then the instance is waiting, and starts when that one ends.
type instance struct {
	topic       string
	key         string
	generation  generation
	policy      hostoperations.TopicPolicy
	values      map[string]*apiv1.TopicEntry
	synced      bool
	waiting     bool
	subscribers map[subscriberID]*subscriber
}

// lifeline is the producer goroutine alive for an instance key. It can outlive
// its instance, because a goroutine cannot be killed and a source may ignore its
// cancellation. A key has at most one, so a producer that never ends costs one
// goroutine however many subscribers come and go. abandoned marks one that was
// asked to stop and has not, past its grace.
type lifeline struct {
	at        generation
	abandoned bool
}

// producerRef names one producer goroutine: its instance and generation.
type producerRef struct {
	topic string
	key   string
	at    generation
}

// subscriber tracks what its client holds against the instance's keys. unsent
// holds keys the client has never had, changed its out-of-date keys, and removed
// its keys that are gone. They hold names only, so the next frame is built from
// the newest value, which is what conflates a slow reader. Each set stays within
// the key bound.
type subscriber struct {
	connection  uint64
	instance    *instance
	delivered   map[string]struct{}
	unsent      map[string]struct{}
	changed     map[string]struct{}
	removed     map[string]struct{}
	syncPending bool
	failure     *apiv1.TopicFailure
}

// effects are the shell's work after a transition.
type effects struct {
	wake []subscriberID
	// start asks the shell to run a producer and to report ended when it returns.
	start *producerRef
	// stop asks the shell to cancel a producer, and to report overdue if it has
	// not ended within the stop grace.
	stop *producerRef
}

func newState(limits limits) *state {
	return &state{
		limits:      limits,
		instances:   map[string]*instance{},
		producers:   map[string]*lifeline{},
		subscribers: map[subscriberID]*subscriber{},
		connections: map[uint64]int{},
	}
}

func (s *state) id() uint64 {
	s.nextID++
	return s.nextID
}

// join admits a subscriber to the instance of key, which starts a producer if
// there is none. A refusal changes nothing: caps, and a key whose earlier
// producer is stuck, which a new one must not overlap.
func (s *state) join(connection uint64, topic, key string, policy hostoperations.TopicPolicy) (subscriberID, effects, *apiv1.TopicFailure) {
	if s.connections[connection] >= s.limits.perConnection {
		return 0, effects{}, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED,
			fmt.Sprintf("connection holds %d subscriptions, the limit", s.limits.perConnection))
	}
	if len(s.subscribers) >= s.limits.total {
		return 0, effects{}, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED,
			fmt.Sprintf("gateway holds %d subscriptions, the limit", s.limits.total))
	}
	var work effects
	current, exists := s.instances[key]
	if !exists {
		earlier := s.producers[key]
		if earlier != nil && earlier.abandoned {
			return 0, effects{}, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED, stuckDetail)
		}
		current = &instance{
			topic: topic, key: key, generation: generation(s.id()), policy: policy, waiting: earlier != nil,
			values: map[string]*apiv1.TopicEntry{}, subscribers: map[subscriberID]*subscriber{},
		}
		s.instances[key] = current
		if !current.waiting {
			work.start = s.launch(current)
		}
	}
	joined := &subscriber{
		connection: connection, instance: current, syncPending: current.synced,
		delivered: map[string]struct{}{}, unsent: make(map[string]struct{}, len(current.values)),
		changed: map[string]struct{}{}, removed: map[string]struct{}{},
	}
	for name := range current.values {
		joined.unsent[name] = struct{}{}
	}
	id := subscriberID(s.id())
	current.subscribers[id] = joined
	s.subscribers[id] = joined
	s.connections[connection]++
	return id, work, nil
}

// stuckDetail is what a subscriber is told while its key's producer will not stop.
const stuckDetail = "an earlier producer of this topic has not stopped"

// launch records the producer an instance starts with.
func (s *state) launch(current *instance) *producerRef {
	s.producers[current.key] = &lifeline{at: current.generation}
	return &producerRef{topic: current.topic, key: current.key, at: current.generation}
}

// leave ends a subscription. The last subscriber of an instance removes it,
// drops its retained map and stops its producer.
func (s *state) leave(id subscriberID) effects {
	left := s.subscribers[id]
	delete(s.subscribers, id)
	delete(left.instance.subscribers, id)
	if s.connections[left.connection]--; s.connections[left.connection] == 0 {
		delete(s.connections, left.connection)
	}
	if s.instances[left.instance.key] == left.instance && len(left.instance.subscribers) == 0 {
		delete(s.instances, left.instance.key)
		return effects{stop: stopOf(left.instance)}
	}
	return effects{}
}

// stopOf is the producer an instance is running, or nil while it waits for one.
func stopOf(gone *instance) *producerRef {
	if gone.waiting {
		return nil
	}
	return &producerRef{topic: gone.topic, key: gone.key, at: gone.generation}
}

func (s *state) live(key string, at generation) *instance {
	if current := s.instances[key]; current != nil && current.generation == at {
		return current
	}
	return nil
}

// put retains the newest value of a key. An equal value changes nothing, and a
// breached bound fails the instance instead of truncating it.
func (s *state) put(key string, at generation, name string, entry *apiv1.TopicEntry, size int) effects {
	current := s.live(key, at)
	if current == nil {
		return effects{}
	}
	if size > current.policy.MaxValueBytes {
		return s.fail(key, at, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED,
			fmt.Sprintf("value of %d bytes exceeds the topic's %d", size, current.policy.MaxValueBytes)))
	}
	old, exists := current.values[name]
	if !exists && len(current.values) >= current.policy.MaxKeys {
		return s.fail(key, at, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED,
			fmt.Sprintf("topic exceeds its %d keys", current.policy.MaxKeys)))
	}
	if exists && proto.Equal(old, entry) {
		return effects{}
	}
	current.values[name] = entry
	wake := make([]subscriberID, 0, len(current.subscribers))
	for id, each := range current.subscribers {
		delete(each.removed, name)
		if _, held := each.delivered[name]; held {
			each.changed[name] = struct{}{}
		} else {
			each.unsent[name] = struct{}{}
		}
		wake = append(wake, id)
	}
	return effects{wake: wake}
}

// remove forgets a key. A subscriber that was never sent it owes nothing, which
// keeps its bookkeeping within the key bound however many keys come and go.
func (s *state) remove(key string, at generation, name string) effects {
	current := s.live(key, at)
	if current == nil {
		return effects{}
	}
	if _, exists := current.values[name]; !exists {
		return effects{}
	}
	delete(current.values, name)
	wake := make([]subscriberID, 0, len(current.subscribers))
	for id, each := range current.subscribers {
		delete(each.unsent, name)
		delete(each.changed, name)
		if _, held := each.delivered[name]; held {
			each.removed[name] = struct{}{}
			wake = append(wake, id)
		}
	}
	return effects{wake: wake}
}

// synced records that the instance's first complete state exists.
func (s *state) synced(key string, at generation) effects {
	current := s.live(key, at)
	if current == nil || current.synced {
		return effects{}
	}
	current.synced = true
	wake := make([]subscriberID, 0, len(current.subscribers))
	for id, each := range current.subscribers {
		each.syncPending = true
		wake = append(wake, id)
	}
	return effects{wake: wake}
}

// fail ends an instance for every subscriber. The instance leaves the table at
// once, so the next subscription starts a fresh producer, not this failed one.
func (s *state) fail(key string, at generation, cause *apiv1.TopicFailure) effects {
	current := s.live(key, at)
	if current == nil {
		return effects{}
	}
	return effects{wake: s.close(current, cause), stop: stopOf(current)}
}

// close removes an instance and gives each of its subscribers the failure.
func (s *state) close(current *instance, cause *apiv1.TopicFailure) []subscriberID {
	delete(s.instances, current.key)
	wake := make([]subscriberID, 0, len(current.subscribers))
	for id, each := range current.subscribers {
		each.failure = cause
		wake = append(wake, id)
	}
	return wake
}

// ended records that a producer goroutine returned, with the failure its return
// means. If its instance still wants it, the source ended on its own and the
// instance fails with that cause. Then the key is free, and an instance that
// waited on it starts.
func (s *state) ended(ref producerRef, cause *apiv1.TopicFailure) effects {
	if life := s.producers[ref.key]; life == nil || life.at != ref.at {
		return effects{}
	}
	delete(s.producers, ref.key)
	var work effects
	if current := s.live(ref.key, ref.at); current != nil {
		work.wake = s.close(current, cause)
	}
	if next := s.instances[ref.key]; next != nil && next.waiting {
		next.waiting = false
		work.start = s.launch(next)
	}
	return work
}

// overdue records that a producer asked to stop has not ended within the grace.
// Nothing can end it, so it stays on record and the key refuses new subscribers
// until it returns. The instance that waited on it fails now, with a typed
// frame, and no later one waits at all.
func (s *state) overdue(ref producerRef) effects {
	life := s.producers[ref.key]
	if life == nil || life.at != ref.at {
		return effects{}
	}
	life.abandoned = true
	if next := s.instances[ref.key]; next != nil && next.waiting {
		return effects{wake: s.close(next, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED, stuckDetail))}
	}
	return effects{}
}

// owed counts the frames the subscriber is owed right now. A flush sends at most
// this many, so a key that changes again while a slow client reads cannot extend
// the flush: it waits for the next one. It is at most twice the key bound, plus
// the snapshot marker and a failure.
func (s *state) owed(id subscriberID) int {
	each := s.subscribers[id]
	count := len(each.removed) + len(each.unsent) + len(each.changed)
	if each.syncPending {
		count++
	}
	if each.failure != nil {
		count++
	}
	return count
}

// next takes the subscriber's next frame, or nil when it is up to date. Removals
// go first, so a client never holds more than the topic's key bound. Keys it has
// never had go before refreshes, and synced between them: values keep moving
// under a fast producer and must not postpone the snapshot.
func (s *state) next(id subscriberID) (*apiv1.SubscribeResponse, bool) {
	each := s.subscribers[id]
	if each.failure != nil {
		return &apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Failed{Failed: each.failure}}, true
	}
	for name := range each.removed {
		delete(each.removed, name)
		delete(each.delivered, name)
		return &apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Removed{Removed: name}}, false
	}
	for name := range each.unsent {
		delete(each.unsent, name)
		each.delivered[name] = struct{}{}
		return &apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Entry{Entry: each.instance.values[name]}}, false
	}
	if each.syncPending {
		each.syncPending = false
		return &apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Synced{Synced: &apiv1.Synced{}}}, false
	}
	for name := range each.changed {
		delete(each.changed, name)
		return &apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Entry{Entry: each.instance.values[name]}}, false
	}
	return nil, false
}

func failure(code apiv1.TopicFailureCode, detail string) *apiv1.TopicFailure {
	return &apiv1.TopicFailure{Code: code, Detail: detail}
}
