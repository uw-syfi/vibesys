package lifecycle

import (
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// A standing allocation of a placement whose capacity is a slot of this machine holds one slot of the
// placement, like a running job, from the moment the scheduler grants it until the allocation ends.
// The slot waits for the scheduler as a job does, so a placement that is full of jobs makes the
// allocation wait, and the jobs that run in the allocation claim nothing more.

// slot is one standing allocation's slot of a placement.
type slot struct {
	id        string
	placement string
	run       core.RunID
	at        time.Time
	seq       uint64
	granted   bool
}

// SlotView is a slot as a reader sees it.
type SlotView struct {
	Slot      string
	Placement string
	// Granted says the scheduler granted the slot, which the allocation holds until it drops it.
	Granted bool
}

// slotIDOf names a slot to the scheduler. The prefix keeps it apart from a job's id, which is a
// store's id and a job number.
func slotIDOf(id string) schedule.ID { return schedule.ID("slot:" + id) }

// slotClaims is what a slot claims: one slot of its placement, shared with the jobs that run there.
func slotClaims(sl slot) []schedule.Claim {
	return []schedule.Claim{{Resource: schedule.PlacementOf(sl.placement), Amount: 1}}
}

func (s *State) wantSlot(e SlotWanted) {
	if slices.ContainsFunc(s.slots, func(sl slot) bool { return sl.id == e.Slot }) {
		return
	}
	s.slotSeq++
	s.slots = append(s.slots, slot{id: e.Slot, placement: e.Placement, run: e.Run, at: e.At, seq: s.slotSeq, granted: e.Restored})
}

func (s *State) dropSlot(id string) {
	s.slots = slices.DeleteFunc(s.slots, func(sl slot) bool { return sl.id == id })
}

// Slots returns the slots standing allocations hold or wait for, in the order they were wanted.
func (s State) Slots() []SlotView {
	out := make([]SlotView, 0, len(s.slots))
	for _, sl := range s.slots {
		out = append(out, SlotView{Slot: sl.id, Placement: sl.placement, Granted: sl.granted})
	}
	return out
}
