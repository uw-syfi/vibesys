package hub

import "github.com/uw-syfi/vibesys/orchestrator/internal/core"

// MaxRemembered bounds how many items a subscription remembers as final or as having lost a delta.
// A subscription can live as long as a client stays connected, and a long run finishes thousands of
// items, so an unbounded set would grow for the life of the connection. Forgetting the oldest
// entry costs at most one stale delta of an item that finished thousands of items ago, and deltas
// are advisory: the stored item supersedes them.
const MaxRemembered = 4096

// itemSet is a set of items that forgets the oldest member once it holds MaxRemembered.
type itemSet struct {
	members map[core.ItemID]struct{}
	order   []core.ItemID
}

func newItemSet() *itemSet { return &itemSet{members: map[core.ItemID]struct{}{}} }

func (s *itemSet) has(id core.ItemID) bool {
	_, ok := s.members[id]
	return ok
}

func (s *itemSet) add(id core.ItemID) {
	if s.has(id) {
		return
	}
	s.members[id] = struct{}{}
	s.order = append(s.order, id)
	for len(s.order) > MaxRemembered {
		delete(s.members, s.order[0])
		s.order = s.order[1:]
	}
	// The slice keeps its head until it is reallocated: copy it down once it is mostly dead.
	if cap(s.order) > 4*MaxRemembered {
		s.order = append([]core.ItemID(nil), s.order...)
	}
}

func (s *itemSet) remove(id core.ItemID) {
	if !s.has(id) {
		return
	}
	delete(s.members, id)
	for i, m := range s.order {
		if m == id {
			s.order = append(s.order[:i], s.order[i+1:]...)
			return
		}
	}
}

func (s *itemSet) size() int { return len(s.members) }
