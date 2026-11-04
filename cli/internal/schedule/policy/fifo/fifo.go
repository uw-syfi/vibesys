// Package fifo is the first-come, first-served scheduling policy: the waiting jobs go in the order
// they arrived, by when they were submitted and then by when the daemon heard of them. With one
// slot it is the daemon's behavior before policies: one job at a time, the oldest queued first.
package fifo

import (
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// Name is the policy's name in the operator config.
const Name = "fifo"

// Policy is the fifo policy.
type Policy struct{}

// New returns the policy.
func New() schedule.Policy { return Policy{} }

// Order implements schedule.Policy: the arrival order, which Step gives it.
func (Policy) Order(waiting []schedule.Candidate, _ schedule.Usage) []schedule.Candidate {
	return slices.Clone(waiting)
}
