// Package fairshare is the scheduling policy that shares the resources between runs. Each run has
// its own queue, in arrival order, and the next job is the head of the queue of the run that used
// the resources the job claims least of late (schedule.Usage), divided by the run's weight. Ties go
// to the job that arrived first. A run that waits uses nothing, so its use falls behind every run
// that starts jobs, and it gets its turn: no run starves another.
//
// A job's run is the run that submitted it first. Jobs submitted with no run form one run of their
// own.
package fairshare

import (
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// Name is the policy's name in the operator config.
const Name = "fair-share"

// Policy is the fair-share policy.
type Policy struct {
	// Weights gives a run a larger share: a run of weight 2 may use twice what a run of weight 1
	// does. A run it does not name, and a weight that is not positive, weigh 1.
	Weights map[core.RunID]float64
}

// New returns the policy with every run of weight 1.
func New() schedule.Policy { return Policy{} }

func (p Policy) weight(run core.RunID) float64 {
	if w := p.Weights[run]; w > 0 {
		return w
	}
	return 1
}

// Order implements schedule.Policy. It orders all the waiting jobs, counting each one it puts
// ahead as used by its run, so that within one step the runs take turns.
func (p Policy) Order(waiting []schedule.Candidate, usage schedule.Usage) []schedule.Candidate {
	var runs []core.RunID
	queues := map[core.RunID][]schedule.Candidate{}
	for _, c := range waiting {
		if _, ok := queues[c.Run]; !ok {
			runs = append(runs, c.Run)
		}
		queues[c.Run] = append(queues[c.Run], c)
	}
	used := map[core.RunID]map[schedule.ResourceID]int{}
	use := func(run core.RunID, r schedule.ResourceID) int { return usage.Of(run, r) + used[run][r] }
	score := func(c schedule.Candidate) float64 {
		total := 0
		for _, claim := range c.Claims {
			total += use(c.Run, claim.Resource)
		}
		return float64(total) / p.weight(c.Run)
	}
	out := make([]schedule.Candidate, 0, len(waiting))
	for len(out) < len(waiting) {
		best := -1
		var bestScore float64
		for i, run := range runs {
			q := queues[run]
			if len(q) == 0 {
				continue
			}
			s := score(q[0])
			// runs is in the order of each run's first arrival, and a queue's head is its oldest,
			// so the earliest head wins a tie when ties are broken by arrival.
			if best < 0 || s < bestScore || (s == bestScore && arrivedBefore(q[0], queues[runs[best]][0])) {
				best, bestScore = i, s
			}
		}
		run := runs[best]
		head := queues[run][0]
		queues[run] = queues[run][1:]
		out = append(out, head)
		if used[run] == nil {
			used[run] = map[schedule.ResourceID]int{}
		}
		for _, claim := range head.Claims {
			used[run][claim.Resource] += max(claim.Amount, 1)
		}
	}
	return out
}

func arrivedBefore(a, b schedule.Candidate) bool {
	if c := a.Submitted.Compare(b.Submitted); c != 0 {
		return c < 0
	}
	return a.Seq < b.Seq
}
