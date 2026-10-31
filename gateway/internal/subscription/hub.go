package subscription

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"google.golang.org/protobuf/proto"
)

// Hub is the thin shell around state: it runs producers and senders, and every
// decision is state's. Producers only touch the map, so a blocked client can
// never hold one up.
type Hub struct {
	clock  clock.Clock
	topics map[string]registered

	mu        sync.Mutex
	state     *state
	wakes     map[subscriberID]chan struct{}
	producers map[string]*running
}

// running is one producer goroutine. done outlives the instance so that a new
// producer for the same instance starts only after this one has ended.
type running struct {
	cancel context.CancelFunc
	done   chan struct{}
}

// New builds a hub serving the given topics. A duplicate name is a wiring defect.
func New(ticks clock.Clock, topics ...Registration) *Hub {
	return newHub(ticks, limits{perConnection: MaxPerConnection, total: MaxPerGateway}, topics...)
}

func newHub(ticks clock.Clock, caps limits, topics ...Registration) *Hub {
	hub := &Hub{
		clock: ticks, topics: map[string]registered{}, state: newState(caps),
		wakes: map[subscriberID]chan struct{}{}, producers: map[string]*running{},
	}
	for _, topic := range topics {
		each := topic.registration()
		if _, duplicate := hub.topics[each.name]; duplicate {
			panic(fmt.Sprintf("topic %s registered twice", each.name))
		}
		hub.topics[each.name] = each
	}
	return hub
}

// Serve sends one subscription until ctx ends, send fails, or a failed frame ends
// it. send may block on a slow client; the keys it skips conflate meanwhile.
// connection identifies the caller's transport for the per-connection cap. A
// failed frame is a normal end, so Serve then returns nil.
func (h *Hub) Serve(ctx context.Context, connection uint64, request *apiv1.SubscribeRequest, send func(*apiv1.SubscribeResponse) error) error {
	topic, err := h.topicOf(request)
	if err != nil {
		return send(&apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Failed{Failed: failureOf(err)}})
	}
	// Equal requests share an instance, including when a topic gains parameters.
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(request)
	if err != nil {
		return err
	}
	key := string(encoded)
	heartbeat := h.clock.NewTicker(HeartbeatInterval)
	defer heartbeat.Stop()
	wake := make(chan struct{}, 1)
	var woken <-chan struct{}
	var flush <-chan time.Time
	if topic.policy.MinInterval > 0 {
		ticker := h.clock.NewTicker(topic.policy.MinInterval)
		defer ticker.Stop()
		flush = ticker.Ticks()
	} else {
		woken = wake
	}
	id, refusal := h.join(connection, key, topic, wake)
	if refusal != nil {
		return send(&apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Failed{Failed: refusal}})
	}
	defer h.leave(id)
	for {
		for {
			frame, terminal := h.next(id)
			if frame == nil {
				break
			}
			if err := send(frame); err != nil {
				return err
			}
			if terminal {
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-woken:
		case <-flush:
		case <-heartbeat.Ticks():
			if err := send(&apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Heartbeat{Heartbeat: &apiv1.Heartbeat{}}}); err != nil {
				return err
			}
		}
	}
}

func (h *Hub) topicOf(request *apiv1.SubscribeRequest) (registered, error) {
	message := request.ProtoReflect()
	arm := message.WhichOneof(message.Descriptor().Oneofs().ByName("topic"))
	if arm == nil {
		return registered{}, Unsupported("request names no topic")
	}
	topic, ok := h.topics[string(arm.Name())]
	if !ok {
		return registered{}, Unsupported(fmt.Sprintf("topic %s has no producer", arm.Name()))
	}
	return topic, nil
}

func (h *Hub) join(connection uint64, key string, topic registered, wake chan struct{}) (subscriberID, *apiv1.TopicFailure) {
	h.mu.Lock()
	defer h.mu.Unlock()
	id, at, started, refusal := h.state.join(connection, key, topic.policy)
	if refusal != nil {
		return 0, refusal
	}
	h.wakes[id] = wake
	if started {
		h.start(key, at, topic)
	}
	return id, nil
}

// start runs a producer, after any previous one for the instance has ended, so
// two producers of an instance never overlap.
func (h *Hub) start(key string, at generation, topic registered) {
	ctx, cancel := context.WithCancel(context.Background())
	previous := h.producers[key]
	self := &running{cancel: cancel, done: make(chan struct{})}
	h.producers[key] = self
	go func() {
		defer close(self.done)
		defer cancel()
		if previous != nil {
			<-previous.done
		}
		if ctx.Err() == nil {
			err := runProducer(ctx, topic, producer{hub: h, key: key, at: at})
			if ctx.Err() == nil {
				h.apply(func(s *state) effects { return s.fail(key, at, failureOf(err)) })
			}
		}
		h.mu.Lock()
		defer h.mu.Unlock()
		if h.producers[key] == self {
			delete(h.producers, key)
		}
	}()
}

// runProducer turns every way a source can end into an error: a panic, a return
// while subscribers remain, or the source's own failure.
func runProducer(ctx context.Context, topic registered, into producer) (err error) {
	defer func() {
		if failed := recover(); failed != nil {
			err = &Failure{Code: apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_INTERNAL, Detail: fmt.Sprintf("panic: %v", failed)}
		}
	}()
	if err := topic.run(ctx, into); err != nil {
		return err
	}
	return errors.New("source stopped")
}

func (h *Hub) leave(id subscriberID) {
	h.mu.Lock()
	defer h.mu.Unlock()
	delete(h.wakes, id)
	h.perform(h.state.leave(id))
}

func (h *Hub) next(id subscriberID) (*apiv1.SubscribeResponse, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.state.next(id)
}

func (h *Hub) apply(transition func(*state) effects) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.perform(transition(h.state))
}

func (h *Hub) perform(work effects) {
	for _, id := range work.wake {
		select {
		case h.wakes[id] <- struct{}{}:
		default:
		}
	}
	if work.stopped {
		if current := h.producers[work.instance]; current != nil {
			current.cancel()
		}
	}
}

// producer is one running source's handle on the hub, fenced by its generation.
type producer struct {
	hub *Hub
	key string
	at  generation
}

// typedSink is where the generic Source meets the type-erased hub. It validates
// here so that a bad value fails the topic with a typed frame, once, and not
// each stream with a transport error.
type typedSink[V proto.Message] struct {
	producer
	entry func(string, V) *apiv1.TopicEntry
}

func (s typedSink[V]) Put(name string, value V) {
	entry := s.entry(name, value)
	if err := protovalidate.Validate(entry); err != nil {
		s.hub.apply(func(state *state) effects {
			return state.fail(s.key, s.at, failedFrame(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED, err.Error()))
		})
		return
	}
	size := proto.Size(value)
	s.hub.apply(func(state *state) effects { return state.put(s.key, s.at, name, entry, size) })
}

func (s typedSink[V]) Remove(name string) {
	s.hub.apply(func(state *state) effects { return state.remove(s.key, s.at, name) })
}

func (s typedSink[V]) Synced() {
	s.hub.apply(func(state *state) effects { return state.synced(s.key, s.at) })
}
