// Package schedule decides which waiting jobs may start, given what the jobs that run hold. It is
// pure: Step is a function of its arguments, with no clock, no I/O and no goroutine, so every rule
// here is tested over generated states.
//
// A job holds claims while it runs. A claim is an amount of a resource, or the whole resource for
// the job alone (exclusive). Resource ids are opaque here: placement:<name> is a slot on a place
// jobs run, and others name what a placement gives a job. Step owns two rules that nothing it is
// given can break:
//
//   - the claims on a resource never add up to more than its capacity, and
//   - an exclusive claim never shares its resource with any other claim.
//
// Around them it applies what it is given: a Policy orders the waiting jobs, and Filters may hold a
// job back. A buggy policy or filter can be unfair, never unsafe. A job that waits keeps its turn:
// a later job does not take a resource an earlier one waits for, so a job that needs a resource for
// itself alone is not starved by a stream of jobs that share it.
//
// A job's exclusive claims may be leased too, so that the daemons that share a resource take turns
// at it; the shell takes the leases. A job refused one waits, keeping its turn, naming the holder
// (core.WaitLeased), until the shell says it may try again.
//
// Every waiting job gets one Decision: Start, Wait with the reason, or Refuse for claims that no
// state of the resources can grant.
package schedule

import (
	"fmt"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// ResourceID names a resource.
type ResourceID string

// DefaultPlacement is the one placement of a daemon whose config names none: the machine.
const DefaultPlacement ResourceID = "placement:default"

// Resource is something jobs claim, and how much of it there is.
type Resource struct {
	ID ResourceID
	// Capacity is how many units of it the claims on it may add up to. It is at least 1.
	Capacity int
}

// Claim is what a job holds of one resource while it runs: Amount units of it, or with Exclusive
// the whole resource, shared with no other claim.
type Claim struct {
	Resource  ResourceID
	Amount    int
	Exclusive bool
}

// ID names a job to the scheduler. It is opaque and unique among the holdings and the waiting.
type ID string

// Holding is a job that holds its claims: it runs, or might, or its end is not recorded yet.
type Holding struct {
	ID     ID
	Claims []Claim
	// Run is the run the job's work is counted for.
	Run core.RunID
}

// Candidate is a job that waits to start.
type Candidate struct {
	ID     ID
	Claims []Claim
	// Run is the run the job's work is counted for, which fair policies share the resources by.
	Run core.RunID
	// Submitted and Seq are the job's place in the arrival order: by when it was submitted, then by
	// Seq, which is unique.
	Submitted time.Time
	Seq       uint64
	// First says the job goes before every candidate that does not, whatever the policy says: it
	// has started before, and its store has it running.
	First bool
	// Leased, when set, says a daemon other than this one, or another job, held the lease of one
	// of the job's exclusive claims when the job last tried to take it. The job waits, keeping its
	// turn, until the caller clears it to try again.
	Leased *LeasedOut
}

// LeasedOut is a lease a job was refused: the resource, and who held its lease, or, when the place
// the lease is kept could not be reached, why.
type LeasedOut struct {
	Resource ResourceID
	Holder   core.LeaseHolder
	Detail   string
}

// Grant is one start the scheduler decided, as the caller keeps it for Usage.
type Grant struct {
	Run    core.RunID
	Claims []Claim
}

// RecentGrants is how many of the last grants count as recent use.
const RecentGrants = 32

// State is what Step decides from.
type State struct {
	Resources []Resource
	Holdings  []Holding
	// Waiting is the jobs that wait to start, in any order: Step puts them in arrival order.
	Waiting []Candidate
	// Recent is the last grants, oldest first, as Remember keeps them.
	Recent []Grant
}

// Remember returns the recent grants with the started candidates added, keeping the last
// RecentGrants.
func Remember(recent []Grant, started ...Candidate) []Grant {
	out := slices.Clone(recent)
	for _, c := range started {
		out = append(out, Grant{Run: c.Run, Claims: slices.Clone(c.Claims)})
	}
	if len(out) > RecentGrants {
		out = out[len(out)-RecentGrants:]
	}
	return out
}

// Usage is how much of each resource each run used lately: the units it holds, plus the units of
// its recent grants.
type Usage map[core.RunID]map[ResourceID]int

// Of returns a run's use of a resource.
func (u Usage) Of(run core.RunID, r ResourceID) int { return u[run][r] }

// UsageOf returns the use of the state's holdings and recent grants.
func UsageOf(s State) Usage {
	u := Usage{}
	add := func(run core.RunID, claims []Claim) {
		for _, c := range claims {
			if u[run] == nil {
				u[run] = map[ResourceID]int{}
			}
			u[run][c.Resource] += max(c.Amount, 1)
		}
	}
	for _, h := range s.Holdings {
		add(h.Run, h.Claims)
	}
	for _, g := range s.Recent {
		add(g.Run, g.Claims)
	}
	return u
}

// Policy orders the waiting jobs: who goes first among those that may start. It is given them in
// arrival order and returns the same jobs in its order. Step starts them in that order while the
// resources allow, so a policy decides fairness and never safety.
type Policy interface {
	Order(waiting []Candidate, usage Usage) []Candidate
}

// Filter is a rule that may hold a job back that the resources would let start. held is what is
// held at that moment, the jobs this Step started included. A Filter that says no gives the
// reason, in words.
type Filter interface {
	Admit(c Candidate, held []Holding) (bool, string)
}

// WaitReason is why a job waits.
type WaitReason struct {
	Kind     core.WaitKind
	Resource ResourceID
	// Blockers are the holdings that stand in the way, or for core.WaitBehind the earlier waiting
	// jobs, in the order Step met them.
	Blockers []ID
	// Detail is the filter's reason for core.WaitHeld, why for a Refuse, and for core.WaitLeased
	// why the lease could not be taken when nobody was found to hold it.
	Detail string
	// Elsewhere is who holds the lease for core.WaitLeased.
	Elsewhere *core.LeaseHolder
}

// Decision is what Step decided for one waiting job. The set is closed.
type Decision interface {
	Target() ID
	decision()
}

// Start says the job may start now.
type Start struct{ ID ID }

// Wait says the job waits, and why.
type Wait struct {
	ID     ID
	Reason WaitReason
}

// Refuse says the job's claims can never be granted: a resource that does not exist, or more of
// one than there is. Detail says which.
type Refuse struct {
	ID     ID
	Detail string
}

// Target implements Decision.
func (d Start) Target() ID { return d.ID }

// Target implements Decision.
func (d Wait) Target() ID { return d.ID }

// Target implements Decision.
func (d Refuse) Target() ID { return d.ID }

func (Start) decision()  {}
func (Wait) decision()   {}
func (Refuse) decision() {}

// ArrivalOrder sorts candidates by when they were submitted, then by Seq.
func ArrivalOrder(cs []Candidate) []Candidate {
	out := slices.Clone(cs)
	slices.SortStableFunc(out, func(a, b Candidate) int {
		if c := a.Submitted.Compare(b.Submitted); c != 0 {
			return c
		}
		switch {
		case a.Seq < b.Seq:
			return -1
		case a.Seq > b.Seq:
			return 1
		}
		return 0
	})
	return out
}

// Step decides, for every waiting job, whether it starts now. The decisions come in the order the
// jobs were considered: the First ones in arrival order, then the rest in the policy's order. A nil
// policy is arrival order.
func Step(s State, p Policy, filters ...Filter) []Decision {
	arrived := ArrivalOrder(s.Waiting)
	var first, rest []Candidate
	for _, c := range arrived {
		if c.First {
			first = append(first, c)
		} else {
			rest = append(rest, c)
		}
	}
	if p != nil && len(rest) > 0 {
		rest = permutation(rest, p.Order(slices.Clone(rest), UsageOf(s)))
	}
	b := newBook(s)
	out := make([]Decision, 0, len(arrived))
	for _, c := range append(first, rest...) {
		out = append(out, b.decide(c, filters))
	}
	return out
}

// permutation returns what the policy ordered, held to being the waiting jobs: each once, any it
// dropped after the rest in arrival order, and any it made up left out.
func permutation(waiting, ordered []Candidate) []Candidate {
	byID := make(map[ID]Candidate, len(waiting))
	for _, c := range waiting {
		byID[c.ID] = c
	}
	seen := make(map[ID]bool, len(waiting))
	out := make([]Candidate, 0, len(waiting))
	for _, c := range ordered {
		if have, ok := byID[c.ID]; ok && !seen[c.ID] {
			seen[c.ID] = true
			out = append(out, have)
		}
	}
	for _, c := range waiting {
		if !seen[c.ID] {
			out = append(out, c)
		}
	}
	return out
}

// book is what is held of each resource while Step decides.
type book struct {
	capacity map[ResourceID]int
	used     map[ResourceID]int
	// holders are the holdings of each resource, and exclusive those that hold it alone.
	holders   map[ResourceID][]ID
	exclusive map[ResourceID][]ID
	// reserved are the waiting jobs that keep their turn on each resource.
	reserved map[ResourceID][]ID
	held     []Holding
}

func newBook(s State) *book {
	b := &book{
		capacity: map[ResourceID]int{}, used: map[ResourceID]int{}, holders: map[ResourceID][]ID{},
		exclusive: map[ResourceID][]ID{}, reserved: map[ResourceID][]ID{}, held: slices.Clone(s.Holdings),
	}
	for _, r := range s.Resources {
		b.capacity[r.ID] = r.Capacity
	}
	for _, h := range s.Holdings {
		b.hold(h.ID, h.Claims)
	}
	return b
}

// need is a job's claims on one resource, merged.
type need struct {
	resource  ResourceID
	amount    int
	exclusive bool
}

// needs merges a job's claims by resource, in the order of their first claim. An amount below 1
// counts as 1: a claim always takes something.
func needs(claims []Claim) []need {
	var out []need
	for _, c := range claims {
		i := slices.IndexFunc(out, func(n need) bool { return n.resource == c.Resource })
		if i < 0 {
			out = append(out, need{resource: c.Resource})
			i = len(out) - 1
		}
		out[i].amount += max(c.Amount, 1)
		out[i].exclusive = out[i].exclusive || c.Exclusive
	}
	return out
}

func (b *book) hold(id ID, claims []Claim) {
	for _, n := range needs(claims) {
		b.used[n.resource] += n.amount
		b.holders[n.resource] = append(b.holders[n.resource], id)
		if n.exclusive {
			b.exclusive[n.resource] = append(b.exclusive[n.resource], id)
		}
	}
}

func (b *book) decide(c Candidate, filters []Filter) Decision {
	ns := needs(c.Claims)
	for _, n := range ns {
		capacity, known := b.capacity[n.resource]
		switch {
		case !known:
			return Refuse{ID: c.ID, Detail: fmt.Sprintf("no resource %q", n.resource)}
		case n.amount > capacity:
			return Refuse{ID: c.ID, Detail: fmt.Sprintf("it claims %d of %s, which has %d", n.amount, n.resource, capacity)}
		}
	}
	if why, blocked := b.blocked(ns); blocked {
		b.reserve(c.ID, ns)
		return Wait{ID: c.ID, Reason: why}
	}
	for _, n := range ns {
		if r := b.reserved[n.resource]; len(r) > 0 {
			b.reserve(c.ID, ns)
			return Wait{ID: c.ID, Reason: WaitReason{Kind: core.WaitBehind, Resource: n.resource, Blockers: slices.Clone(r)}}
		}
	}
	if l := c.Leased; l != nil {
		b.reserve(c.ID, ns)
		holder := l.Holder
		return Wait{ID: c.ID, Reason: WaitReason{Kind: core.WaitLeased, Resource: l.Resource, Elsewhere: &holder, Detail: l.Detail}}
	}
	for _, f := range filters {
		if ok, why := f.Admit(c, slices.Clone(b.held)); !ok {
			return Wait{ID: c.ID, Reason: WaitReason{Kind: core.WaitHeld, Detail: why}}
		}
	}
	b.hold(c.ID, c.Claims)
	b.held = append(b.held, Holding{ID: c.ID, Claims: slices.Clone(c.Claims), Run: c.Run})
	return Start{ID: c.ID}
}

// blocked says what the holdings keep a job with these needs from.
func (b *book) blocked(ns []need) (WaitReason, bool) {
	for _, n := range ns {
		holders := b.holders[n.resource]
		switch {
		case len(b.exclusive[n.resource]) > 0:
			return WaitReason{Kind: core.WaitHeldExclusively, Resource: n.resource, Blockers: slices.Clone(b.exclusive[n.resource])}, true
		case n.exclusive && len(holders) > 0:
			return WaitReason{Kind: core.WaitInUse, Resource: n.resource, Blockers: slices.Clone(holders)}, true
		case b.used[n.resource]+n.amount > b.capacity[n.resource]:
			return WaitReason{Kind: core.WaitFull, Resource: n.resource, Blockers: slices.Clone(holders)}, true
		}
	}
	return WaitReason{}, false
}

// reserve keeps a waiting job's turn on what it needs.
func (b *book) reserve(id ID, ns []need) {
	for _, n := range ns {
		b.reserved[n.resource] = append(b.reserved[n.resource], id)
	}
}

// Demand says what a job claims while it runs.
type Demand interface {
	Claims(job core.Job) []Claim
}

// DefaultDemand is the demand of a daemon whose config names no placement: every job takes one
// slot of DefaultPlacement, and a measure job, whose benchmark samples must not be disturbed, takes
// the whole placement.
type DefaultDemand struct{}

// Claims implements Demand.
func (DefaultDemand) Claims(job core.Job) []Claim {
	exclusive := job.Spec.Settings != nil && job.Spec.Settings.Kind() == core.JobMeasure
	return []Claim{{Resource: DefaultPlacement, Amount: 1, Exclusive: exclusive}}
}
