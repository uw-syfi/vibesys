// Package hub carries the daemon's live view to the clients watching it.
//
// Three kinds of thing pass through. A latest value is a whole search view with one
// slot per run per subscription, outside lag accounting. A stored event has a sequence and is never lost: a client that
// cannot keep up is told to start again from a fresh snapshot, which holds everything the events it
// missed would have said. A delta is progress on a running item that is stored nowhere, so it may
// be merged with the deltas next to it, dropped, or superseded, and the next stored version of its
// item says all of it over again.
//
// The hub reads no clock and starts nothing. The caller ends a merge window by calling Flush, so a
// test or a simulation decides when a window ends. The writer never waits on a client: Publish
// costs one pass over the subscriptions, each of which holds a bounded queue.
package hub

import (
	"sort"
	"sync"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The default bounds on what one client may hold unread.
const (
	DefaultMaxItems = 1000
	DefaultMaxBytes = 8 << 20
)

// Limits bound one subscription's unread queue. Going over either is a lag.
type Limits struct{ MaxItems, MaxBytes int }

// DefaultLimits is 1,000 queued items or 8 MiB, whichever comes first.
var DefaultLimits = Limits{MaxItems: DefaultMaxItems, MaxBytes: DefaultMaxBytes}

// Stored is what watchers receive of a committed event, on its way to them: its public part, so that
// nothing the host keeps for itself is in the hub at all.
type Stored struct {
	Change *pb.RunChange
	// Agent is the agent the event concerns, or empty when it concerns none in particular. A
	// subscription to one agent receives only its events.
	Agent core.AgentID
	// Item is set when the event is a new version of an item.
	Item core.ItemID
	// ItemFinal says that version is the last one of its item.
	ItemFinal bool
}

// Interest says what a subscription wants.
type Interest struct {
	// Everything asks for every stored event and no delta.
	Everything bool
	// Agent asks for one agent's stored events and its deltas. Ignored when Everything is set.
	Agent core.AgentID
}

// Message is one thing for a client: exactly one field is set.
type Message struct {
	Stored *Stored
	Delta  *pb.Delta
	Search *pb.SearchState
	// size is what the message counts against its subscription's bounds.
	size int
}

// Hub fans events out to subscriptions.
type Hub struct {
	limits Limits
	mu     sync.Mutex
	subs   map[*Subscription]struct{}
}

// New returns a hub whose subscriptions hold at most limits.
func New(limits Limits) *Hub {
	if limits.MaxItems <= 0 || limits.MaxBytes <= 0 {
		panic("hub: limits must be positive")
	}
	return &Hub{limits: limits, subs: map[*Subscription]struct{}{}}
}

// Subscribe starts a subscription that sees everything published after it.
func (h *Hub) Subscribe(in Interest) *Subscription {
	s := &Subscription{
		hub: h, interest: in, wake: make(chan struct{}, 1),
		latest: map[core.RunID]*pb.SearchState{}, revisions: map[core.RunID]uint64{},
		pending: map[deltaKey]*pendingDelta{}, broken: newItemSet(), final: newItemSet(),
	}
	h.mu.Lock()
	h.subs[s] = struct{}{}
	h.mu.Unlock()
	return s
}

// PublishStored hands a committed event to the subscriptions that want it. Events must be
// published in sequence order.
func (h *Hub) PublishStored(st Stored) {
	size := proto.Size(st.Change)
	h.mu.Lock()
	defer h.mu.Unlock()
	for s := range h.subs {
		if s.wantsStored(st) {
			s.addStored(st, size, h.limits)
		}
	}
}

// PublishLatest replaces a run's unread whole view only with a newer revision.
// It never consumes queue capacity, even while lagged. Only run watchers receive it.
// The hub owns a copy, so callers may reuse their input after publishing.
func (h *Hub) PublishLatest(run core.RunID, state *pb.SearchState) {
	if state == nil || state.GetRunId() != string(run) || state.GetRevision() == 0 {
		panic("hub: invalid latest identity")
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	for s := range h.subs {
		if s.interest.Everything && state.Revision > s.revisions[run] {
			s.revisions[run] = state.Revision
			s.latest[run] = proto.Clone(state).(*pb.SearchState)
			s.signal()
		}
	}
}

// PublishDelta hands live progress to the subscriptions watching its agent.
func (h *Hub) PublishDelta(d *pb.Delta) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for s := range h.subs {
		if !s.interest.Everything && s.interest.Agent == core.AgentID(d.GetAgentId()) {
			s.addDelta(d, h.limits)
		}
	}
}

// Flush ends the merge window: every delta merged so far becomes readable.
func (h *Hub) Flush() {
	h.mu.Lock()
	defer h.mu.Unlock()
	for s := range h.subs {
		s.flush()
	}
}

// Subscribers counts the open subscriptions.
func (h *Hub) Subscribers() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.subs)
}

type deltaKey struct {
	item core.ItemID
	// output says the delta is command output and not text.
	output bool
}

type pendingDelta struct {
	delta *pb.Delta
	size  int
}

// Subscription is one client's queue. Its methods are safe to call from the client's goroutine
// while the writer publishes.
type Subscription struct {
	hub      *Hub
	interest Interest
	wake     chan struct{}

	// The fields below are guarded by hub.mu.
	latest    map[core.RunID]*pb.SearchState
	revisions map[core.RunID]uint64
	ready     []Message
	pending   map[deltaKey]*pendingDelta
	order     []deltaKey
	// broken names items that lost a delta, so that later deltas would build on a gap. They are
	// ignored until the item's next stored version.
	broken *itemSet
	// final names items whose last version was sent, so that a late delta is not shown after it.
	final  *itemSet
	count  int
	bytes  int
	lagged bool
}

// Ready is signalled when Take would return something. A signal may be spurious.
func (s *Subscription) Ready() <-chan struct{} { return s.wake }

// Take returns what is readable, oldest first, and empties the queue. Lagged says the client fell
// behind its bounds: the queue was dropped, and the client must read a fresh snapshot, then call
// Resume. While lagged, only latest values are queued.
func (s *Subscription) Take() (messages []Message, lagged bool) {
	s.hub.mu.Lock()
	defer s.hub.mu.Unlock()
	if s.lagged {
		return nil, true
	}
	messages, s.ready = s.ready, nil
	for _, m := range messages {
		s.count--
		s.bytes -= m.size
	}
	keys := make([]core.RunID, 0, len(s.latest))
	for run := range s.latest {
		keys = append(keys, run)
	}
	sort.Slice(keys, func(i, j int) bool { return keys[i] < keys[j] })
	for _, run := range keys {
		messages = append(messages, Message{Search: s.latest[run]})
		delete(s.latest, run)
	}
	return messages, false
}

// Resume ends a lag. The caller holds whatever keeps the writer from publishing between reading
// its snapshot and calling this, so that nothing is missed or repeated.
func (s *Subscription) Resume() {
	s.hub.mu.Lock()
	defer s.hub.mu.Unlock()
	s.lagged = false
}

// Close ends the subscription.
func (s *Subscription) Close() {
	s.hub.mu.Lock()
	defer s.hub.mu.Unlock()
	delete(s.hub.subs, s)
}

func (s *Subscription) signal() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

func (s *Subscription) wantsStored(st Stored) bool {
	return s.interest.Everything || (s.interest.Agent != "" && s.interest.Agent == st.Agent)
}

// lag drops everything queued, because the client must start from a snapshot anyway.
func (s *Subscription) lag() {
	s.ready, s.pending, s.order = nil, map[deltaKey]*pendingDelta{}, nil
	s.broken, s.final = newItemSet(), newItemSet()
	s.count, s.bytes = 0, 0
	s.lagged = true
	s.signal()
}

func (s *Subscription) addStored(st Stored, size int, l Limits) {
	if s.lagged {
		return
	}
	if st.Item != "" {
		s.supersede(st.Item)
		s.broken.remove(st.Item)
		if st.ItemFinal {
			s.final.add(st.Item)
		} else {
			s.final.remove(st.Item)
		}
	}
	if s.count+1 > l.MaxItems || s.bytes+size > l.MaxBytes {
		s.lag()
		return
	}
	s.ready = append(s.ready, Message{Stored: &st, size: size})
	s.count++
	s.bytes += size
	s.signal()
}

// supersede removes the deltas of an item that a stored version now says in full: showing one after
// it would show text the item already holds.
func (s *Subscription) supersede(item core.ItemID) {
	for _, output := range []bool{false, true} {
		key := deltaKey{item, output}
		if p, ok := s.pending[key]; ok {
			delete(s.pending, key)
			s.count--
			s.bytes -= p.size
		}
	}
	s.order = filterKeys(s.order, item)
	kept := s.ready[:0:0]
	for _, m := range s.ready {
		if m.Delta != nil && core.ItemID(m.Delta.GetItemId()) == item {
			s.count--
			s.bytes -= m.size
			continue
		}
		kept = append(kept, m)
	}
	s.ready = kept
}

func filterKeys(keys []deltaKey, item core.ItemID) []deltaKey {
	out := keys[:0:0]
	for _, k := range keys {
		if k.item != item {
			out = append(out, k)
		}
	}
	return out
}

func (s *Subscription) addDelta(d *pb.Delta, l Limits) {
	item := core.ItemID(d.GetItemId())
	if s.final.has(item) || s.lagged {
		return
	}
	if s.broken.has(item) {
		return
	}
	_, output := d.GetBody().(*pb.Delta_CommandOutput)
	key := deltaKey{item, output}
	if p, ok := s.pending[key]; ok {
		extra := len(d.GetText()) + len(d.GetCommandOutput())
		if s.bytes+extra > l.MaxBytes {
			s.drop(item)
			return
		}
		merged := proto.Clone(p.delta).(*pb.Delta)
		if key.output {
			merged.Body = &pb.Delta_CommandOutput{CommandOutput: merged.GetCommandOutput() + d.GetCommandOutput()}
		} else {
			merged.Body = &pb.Delta_Text{Text: merged.GetText() + d.GetText()}
		}
		p.delta, p.size = merged, p.size+extra
		s.bytes += extra
		return
	}
	size := proto.Size(d)
	if s.count+1 > l.MaxItems || s.bytes+size > l.MaxBytes {
		s.drop(item)
		return
	}
	s.pending[key] = &pendingDelta{delta: d, size: size}
	s.order = append(s.order, key)
	s.count++
	s.bytes += size
}

// drop gives up on an item's deltas until its next stored version.
func (s *Subscription) drop(item core.ItemID) {
	s.broken.add(item)
	s.supersede(item)
}

func (s *Subscription) flush() {
	if len(s.order) == 0 {
		return
	}
	for _, key := range s.order {
		p := s.pending[key]
		size := proto.Size(p.delta)
		s.bytes += size - p.size
		s.ready = append(s.ready, Message{Delta: p.delta, size: size})
	}
	s.pending, s.order = map[deltaKey]*pendingDelta{}, nil
	s.signal()
}
