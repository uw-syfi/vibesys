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
// instance, and what each subscriber still owes its client. It holds no
// goroutines, locks or clocks. Each transition returns what the shell must do.
//
// Memory is O(keys) per subscriber and retains one value per key: a slow
// subscriber costs key names, never a queue of values.
type state struct {
	limits      limits
	instances   map[string]*instance
	subscribers map[subscriberID]*subscriber
	connections map[uint64]int
	nextID      uint64
}

// instance is one topic instance: exactly one producer while it is in instances.
type instance struct {
	key         string
	generation  generation
	policy      hostoperations.TopicPolicy
	values      map[string]*apiv1.TopicEntry
	synced      bool
	subscribers map[subscriberID]*subscriber
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
	// stopped asks the shell to end the producer of instance.
	stopped  bool
	instance string
}

func newState(limits limits) *state {
	return &state{
		limits:      limits,
		instances:   map[string]*instance{},
		subscribers: map[subscriberID]*subscriber{},
		connections: map[uint64]int{},
	}
}

func (s *state) id() uint64 {
	s.nextID++
	return s.nextID
}

// join admits a subscriber. started reports a new instance, whose producer the
// shell must run under the returned generation. A refusal changes nothing.
func (s *state) join(connection uint64, key string, policy hostoperations.TopicPolicy) (subscriberID, generation, bool, *apiv1.TopicFailure) {
	if s.connections[connection] >= s.limits.perConnection {
		return 0, 0, false, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED,
			fmt.Sprintf("connection holds %d subscriptions, the limit", s.limits.perConnection))
	}
	if len(s.subscribers) >= s.limits.total {
		return 0, 0, false, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED,
			fmt.Sprintf("gateway holds %d subscriptions, the limit", s.limits.total))
	}
	current, exists := s.instances[key]
	if !exists {
		current = &instance{
			key: key, generation: generation(s.id()), policy: policy,
			values: map[string]*apiv1.TopicEntry{}, subscribers: map[subscriberID]*subscriber{},
		}
		s.instances[key] = current
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
	return id, current.generation, !exists, nil
}

// leave ends a subscription. The last subscriber of a live instance stops its
// producer and drops its retained map.
func (s *state) leave(id subscriberID) effects {
	left := s.subscribers[id]
	delete(s.subscribers, id)
	delete(left.instance.subscribers, id)
	if s.connections[left.connection]--; s.connections[left.connection] == 0 {
		delete(s.connections, left.connection)
	}
	if s.instances[left.instance.key] == left.instance && len(left.instance.subscribers) == 0 {
		delete(s.instances, left.instance.key)
		return effects{stopped: true, instance: left.instance.key}
	}
	return effects{}
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
	delete(s.instances, key)
	wake := make([]subscriberID, 0, len(current.subscribers))
	for id, each := range current.subscribers {
		each.failure = cause
		wake = append(wake, id)
	}
	return effects{wake: wake, stopped: true, instance: key}
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
