package subscription

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// Hub is the thin shell around state: it runs producers and senders, and every
// decision is state's. Producers only touch the map, so a blocked client can
// never hold one up.
type Hub struct {
	clock  clock.Clock
	topics map[string]registered

	mu      sync.Mutex
	state   *state
	wakes   map[subscriberID]chan struct{}
	running map[producerRef]*running
}

// running is the shell's handle on one producer goroutine: how to cancel it, and
// a signal that it has returned.
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
		wakes: map[subscriberID]chan struct{}{}, running: map[producerRef]*running{},
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
	// A field this schema does not know is no routing key, and would split an
	// instance, so a request that carries one is refused.
	if unknown := unknownField(request.ProtoReflect()); unknown != "" {
		return send(&apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Failed{Failed: failureOf(Unsupported("request has an unknown field " + unknown))}})
	}
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(request)
	if err != nil {
		return err
	}
	key := string(encoded)
	heartbeat := h.clock.NewTicker(HeartbeatInterval)
	defer heartbeat.Stop()
	wake := make(chan struct{}, 1)
	id, refusal := h.join(connection, key, topic, wake)
	if refusal != nil {
		return send(&apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Failed{Failed: refusal}})
	}
	defer h.leave(id)
	beat := &apiv1.SubscribeResponse{Frame: &apiv1.SubscribeResponse_Heartbeat{Heartbeat: &apiv1.Heartbeat{}}}
	// A flush is due at once, unless the last one was less than the topic's minimum
	// interval ago: a quiet stream is answered without delay, and a busy one never
	// faster than the floor. The snapshot is exempt, so a subscriber is never held back
	// from the state it asked for; the floor spaces the changes after it.
	var last time.Time
	snapshotted := false
	counted := func(frame *apiv1.SubscribeResponse) error {
		snapshotted = snapshotted || frame.GetSynced() != nil
		return send(frame)
	}
	for {
		if wait := topic.policy.MinInterval - h.clock.Now().Sub(last); snapshotted && !last.IsZero() && wait > 0 {
			pause := h.clock.NewTicker(wait)
			select {
			case <-ctx.Done():
				pause.Stop()
				return ctx.Err()
			case <-pause.Ticks():
			case <-heartbeat.Ticks():
				if err := send(beat); err != nil {
					pause.Stop()
					return err
				}
			}
			pause.Stop()
			continue
		}
		// What changes from here on signals again, so a wake is never lost.
		select {
		case <-wake:
		default:
		}
		sent, ended, err := h.flush(id, counted)
		if ended || err != nil {
			return err
		}
		if sent && snapshotted {
			last = h.clock.Now()
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-wake:
		case <-heartbeat.Ticks():
			if err := send(beat); err != nil {
				return err
			}
		}
	}
}

// flush sends what the subscriber is owed when it starts, and no more: a client
// slower than the producer finds its keys changed again as it reads, and a flush
// that chased them would never end, so neither the interval nor a heartbeat could
// apply. What changes meanwhile waits for the next flush. sent reports whether any
// frame went, and ended a terminal frame, after which Serve returns.
func (h *Hub) flush(id subscriberID, send func(*apiv1.SubscribeResponse) error) (sent, ended bool, err error) {
	for range h.owed(id) {
		frame, terminal := h.next(id)
		if frame == nil {
			return sent, false, nil
		}
		if err := send(frame); err != nil {
			return sent, true, err
		}
		sent = true
		if terminal {
			return sent, true, nil
		}
	}
	return sent, false, nil
}

// unknownField names the first field a message, or a message inside it, holds
// that its schema does not define, or returns "" if there is none.
func unknownField(message protoreflect.Message) string {
	if raw := message.GetUnknown(); len(raw) > 0 {
		number, _, _ := protowire.ConsumeTag(raw)
		return fmt.Sprintf("%d of %s", number, message.Descriptor().FullName())
	}
	var found string
	within := func(inner protoreflect.Message) bool {
		found = unknownField(inner)
		return found == ""
	}
	message.Range(func(field protoreflect.FieldDescriptor, value protoreflect.Value) bool {
		switch {
		case field.IsList() && field.Message() != nil:
			for index := range value.List().Len() {
				if !within(value.List().Get(index).Message()) {
					return false
				}
			}
		case field.IsMap() && field.MapValue().Message() != nil:
			value.Map().Range(func(_ protoreflect.MapKey, entry protoreflect.Value) bool { return within(entry.Message()) })
		case !field.IsMap() && field.Message() != nil:
			within(value.Message())
		}
		return found == ""
	})
	return found
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
	id, work, refusal := h.state.join(connection, topic.name, key, topic.policy)
	if refusal != nil {
		return 0, refusal
	}
	h.wakes[id] = wake
	h.perform(work)
	return id, nil
}

// start runs a producer. The core starts one only when no other is alive for
// the key, so none overlaps, and nothing here waits for another.
func (h *Hub) start(ref producerRef) {
	ctx, cancel := context.WithCancel(context.Background())
	self := &running{cancel: cancel, done: make(chan struct{})}
	h.running[ref] = self
	topic := h.topics[ref.topic]
	sign := new(atomic.Int64)
	sign.Store(h.clock.Now().UnixNano())
	if lease := topic.policy.SourceLease; lease > 0 {
		go h.holdToLease(ctx, ref, sign, lease)
	}
	go func() {
		// The end is reported from a defer, so a source that exits its goroutine
		// without returning (runtime.Goexit) still frees the key and fails its
		// subscribers, instead of leaving them heartbeats and nothing else.
		err := errors.New("source exited without returning")
		defer func() {
			cancel()
			h.apply(func(s *state) effects {
				delete(h.running, ref)
				close(self.done)
				return s.ended(ref, failureOf(err))
			})
		}()
		err = runProducer(ctx, topic, producer{hub: h, key: ref.key, at: ref.at, sign: sign})
	}()
}

// holdToLease ends the topic when its source has given no sign of life for a lease. The
// heartbeats are the hub's: they prove the stream and say nothing of the source, so a source
// stuck in a read would otherwise leave every subscriber a live stream of a state that no longer
// changes. The check runs four times a lease, so the end comes within a quarter lease after it.
func (h *Hub) holdToLease(ctx context.Context, ref producerRef, sign *atomic.Int64, lease time.Duration) {
	checks := h.clock.NewTicker(lease / 4)
	defer checks.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-checks.Ticks():
			if h.clock.Now().Sub(time.Unix(0, sign.Load())) <= lease {
				continue
			}
			detail := fmt.Sprintf("the source gave no sign of life for %s", lease)
			h.apply(func(s *state) effects {
				return s.fail(ref.key, ref.at, failure(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED, detail))
			})
			return
		}
	}
}

// stop cancels a producer. A source may ignore that, and a goroutine cannot be
// killed, so a producer still running after StopGrace is reported to the core,
// which stops waiting for it.
func (h *Hub) stop(ref producerRef) {
	self := h.running[ref]
	self.cancel()
	go func() {
		grace := h.clock.NewTicker(StopGrace)
		defer grace.Stop()
		select {
		case <-self.done:
		case <-grace.Ticks():
			h.apply(func(s *state) effects { return s.overdue(ref) })
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

func (h *Hub) owed(id subscriberID) int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.state.owed(id)
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
	if work.stop != nil {
		h.stop(*work.stop)
	}
	if work.start != nil {
		h.start(*work.start)
	}
}

// producer is one running source's handle on the hub, fenced by its generation.
type producer struct {
	hub *Hub
	key string
	at  generation
	// sign is when the source last showed it was alive, on the hub's clock in nanoseconds.
	sign *atomic.Int64
}

// alive records a sign of life.
func (p producer) alive() { p.sign.Store(p.hub.clock.Now().UnixNano()) }

// typedSink is where the generic Source meets the type-erased hub. It checks here
// that an entry can reach a client, so that a bad value fails the topic with a
// typed frame, once, and not each stream with a transport error.
type typedSink[V proto.Message] struct {
	producer
	entry func(string, V) *apiv1.TopicEntry
}

func (s typedSink[V]) Put(name string, value V) {
	s.alive()
	entry := s.entry(name, value)
	if err := Sendable(entry); err != nil {
		s.hub.apply(func(state *state) effects {
			return state.fail(s.key, s.at, failedFrame(apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_SOURCE_FAILED, err.Error()))
		})
		return
	}
	size := proto.Size(value)
	s.hub.apply(func(state *state) effects { return state.put(s.key, s.at, name, entry, size) })
}

// Sendable says whether a message passes the schema's rules and marshals. The
// rules do not see invalid UTF-8 in a string, and marshalling refuses it, which
// on the wire ends a stream with a transport error for every subscriber. The
// error's text can quote the message.
func Sendable(message proto.Message) error {
	if err := protovalidate.Validate(message); err != nil {
		return err
	}
	_, err := proto.Marshal(message)
	return err
}

func (s typedSink[V]) Remove(name string) {
	s.alive()
	s.hub.apply(func(state *state) effects { return state.remove(s.key, s.at, name) })
}

func (s typedSink[V]) Synced() {
	s.alive()
	s.hub.apply(func(state *state) effects { return state.synced(s.key, s.at) })
}

func (s typedSink[V]) Alive() { s.alive() }
