package alloc_test

import (
	"fmt"
	"reflect"
	"slices"
	"sort"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// model is a reference of the state machine written apart from the core, the plainest way the
// design reads, with maps and no phases: run beside Step over the same events, it must agree after
// every step on which requests are refused (and why, by type), which allocations exist, their
// stages, the job each runs and the jobs it queues, and the successors orders made.
type model struct {
	cfg   alloc.Config
	m     map[alloc.ID]*mAlloc
	n     int
	ended map[core.RunID]bool
}

type mJob struct {
	id      core.JobID
	bound   time.Duration
	started time.Time
}

type mAlloc struct {
	id                alloc.ID
	n                 int
	run               core.RunID
	placement         string
	stage             alloc.Stage
	forD              time.Duration
	at                time.Time
	handled           bool
	startedAt, endsAt time.Time
	current           *mJob
	queue             []mJob
	renewal           *alloc.RenewalOrder
	renewedBy         alloc.ID
}

func newModel(cfg alloc.Config) *model {
	return &model{cfg: cfg, m: map[alloc.ID]*mAlloc{}, ended: map[core.RunID]bool{}}
}

func (md *model) sorted() []*mAlloc {
	out := make([]*mAlloc, 0, len(md.m))
	for _, a := range md.m {
		out = append(out, a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].n < out[j].n })
	return out
}

func (md *model) held(run core.RunID, p string) int {
	k := 0
	for _, a := range md.m {
		if a.run == run && a.placement == p && a.stage != alloc.StageEnded {
			k++
		}
	}
	return k
}

func (md *model) create(run core.RunID, p string, d time.Duration, now time.Time, o *alloc.RenewalOrder) alloc.ID {
	md.n++
	id := alloc.ID(fmt.Sprintf("a%d", md.n))
	md.m[id] = &mAlloc{id: id, n: md.n, run: run, placement: p, stage: alloc.StagePending, forD: d, at: now, renewal: o}
	return id
}

func (md *model) live(a *mAlloc) bool {
	return a.stage == alloc.StageRunning || a.stage == alloc.StageDraining
}

func (md *model) endIt(a *mAlloc) {
	a.stage, a.current, a.queue = alloc.StageEnded, nil, nil
}

func (md *model) ahead(a *mAlloc, now time.Time) time.Duration {
	var d time.Duration
	if c := a.current; c != nil {
		if c.started.IsZero() {
			d += c.bound
		} else if left := c.started.Add(c.bound).Sub(now); left > 0 {
			d += left
		}
	}
	for _, q := range a.queue {
		d += q.bound
	}
	return d
}

func (md *model) fits(a *mAlloc, ahead, bound time.Duration, now time.Time) bool {
	if a.endsAt.IsZero() {
		return true
	}
	return ahead+bound <= a.endsAt.Sub(now)-md.cfg.Placements[a.placement].Margin
}

func (md *model) holds(job core.JobID) bool {
	for _, a := range md.m {
		if a.current != nil && a.current.id == job || slices.ContainsFunc(a.queue, func(q mJob) bool { return q.id == job }) {
			return true
		}
	}
	return false
}

// step applies the event, and returns the type of the refusal, or nil for none.
func (md *model) step(now time.Time, e alloc.Event) reflect.Type {
	r := md.apply(now, e)
	if r != nil {
		return reflect.TypeOf(r)
	}
	md.settle(now)
	return nil
}

func (md *model) owned(run core.RunID, id alloc.ID) (*mAlloc, alloc.Refusal) {
	a, ok := md.m[id]
	if !ok {
		return nil, alloc.UnknownAllocation{}
	}
	if a.run != run {
		return nil, alloc.NotOwnedByRun{}
	}
	return a, nil
}

func (md *model) apply(now time.Time, e alloc.Event) alloc.Refusal {
	switch e := e.(type) {
	case alloc.Ask:
		lim, ok := md.cfg.Placements[e.Placement]
		switch {
		case md.ended[e.Run]:
			return alloc.RunNotLive{}
		case !ok:
			return alloc.NotEnabled{}
		case e.For <= 0:
			return alloc.ForNotPositive{}
		case e.For > lim.MaxFor:
			return alloc.ForExceedsMaxFor{}
		case md.held(e.Run, e.Placement) >= lim.MaxHeld:
			return alloc.MaxHeldReached{}
		}
		md.create(e.Run, e.Placement, e.For, now, nil)
	case alloc.EndAsk:
		a, r := md.owned(e.Run, e.ID)
		if r != nil {
			return r
		}
		work := a.current != nil || len(a.queue) > 0
		switch {
		case a.stage == alloc.StageRunning && e.AfterJobs && work:
			a.stage = alloc.StageDraining
		case a.stage == alloc.StageDraining && e.AfterJobs:
		case a.stage != alloc.StageEnded:
			md.endIt(a)
		}
	case alloc.RenewAsk:
		a, r := md.owned(e.Run, e.ID)
		if r != nil {
			return r
		}
		lim, ok := md.cfg.Placements[a.placement]
		o := e.Order
		switch {
		case md.ended[e.Run]:
			return alloc.RunNotLive{}
		case a.stage != alloc.StagePending && a.stage != alloc.StageRunning:
			return alloc.NotRenewable{}
		case a.renewedBy != "":
			return alloc.AlreadyRenewed{}
		case !ok:
			return alloc.NotEnabled{}
		case lim.Endless:
			return alloc.NoEnd{}
		case o.Lead <= 0:
			return alloc.LeadNotPositive{}
		case o.For <= 0:
			return alloc.ForNotPositive{}
		case o.For > lim.MaxFor:
			return alloc.ForExceedsMaxFor{}
		case o.Lead >= o.For:
			return alloc.LeadNotShorterThanFor{}
		case o.Until == nil:
			return alloc.NoUntil{}
		}
		if u, ok := o.Until.(alloc.UntilTime); ok && !u.At.After(now) {
			return alloc.UntilPassed{}
		}
		a.renewal = &o
	case alloc.RenewOffAsk:
		a, r := md.owned(e.Run, e.ID)
		if r != nil {
			return r
		}
		a.renewal = nil
	case alloc.RunEnded:
		md.ended[e.Run] = true
		for _, a := range md.m {
			if a.run == e.Run && a.stage != alloc.StageEnded {
				md.endIt(a)
			}
		}
	case alloc.JobSubmitted:
		switch {
		case md.ended[e.Run]:
			return alloc.RunNotLive{}
		case e.Bound <= 0:
			return alloc.BoundNotPositive{}
		case md.holds(e.Job):
			return alloc.DuplicateJob{}
		}
		if e.Into == "" {
			var best *mAlloc
			var bestAhead time.Duration
			for _, a := range md.sorted() {
				if a.stage != alloc.StageRunning || a.run != e.Run || !slices.Contains(e.Eligible, a.placement) {
					continue
				}
				ahead := md.ahead(a, now)
				if !md.fits(a, ahead, e.Bound, now) {
					continue
				}
				if best == nil || ahead < bestAhead || ahead == bestAhead && endsBefore(a.endsAt, best.endsAt) {
					best, bestAhead = a, ahead
				}
			}
			if best != nil {
				best.queue = append(best.queue, mJob{id: e.Job, bound: e.Bound})
			}
			return nil
		}
		a, r := md.owned(e.Run, e.Into)
		if r != nil {
			return r
		}
		switch {
		case !slices.Contains(e.Eligible, a.placement):
			return alloc.NotEligible{}
		case a.stage != alloc.StageRunning:
			return alloc.NotRunning{}
		case !md.fits(a, md.ahead(a, now), e.Bound, now):
			return alloc.DoesNotFit{}
		}
		a.queue = append(a.queue, mJob{id: e.Job, bound: e.Bound})
	case alloc.JobStarted:
		for _, a := range md.m {
			if a.current != nil && a.current.id == e.Job && a.current.started.IsZero() {
				a.current.started = now
			}
		}
	case alloc.JobFinished:
		for _, a := range md.m {
			if a.current != nil && a.current.id == e.Job {
				a.current = nil
			}
			a.queue = slices.DeleteFunc(a.queue, func(q mJob) bool { return q.id == e.Job })
		}
	case alloc.Submitted:
		if a, ok := md.m[e.ID]; ok && !a.handled && e.Handle.Ref != "" && e.Handle.Alloc == e.ID {
			a.handled = true
		}
	case alloc.SubmitRefused:
		if a, ok := md.m[e.ID]; ok && a.stage == alloc.StagePending && !a.handled {
			md.endIt(a)
		}
	case alloc.Observed:
		a, ok := md.m[e.ID]
		if !ok {
			return nil
		}
		lim := md.cfg.Placements[a.placement]
		switch o := e.Seen.(type) {
		case alloc.SeenPending:
			if a.stage == alloc.StagePending && lim.QueueTimeout > 0 && now.Sub(a.at) >= lim.QueueTimeout {
				md.endIt(a)
			}
		case alloc.SeenRunning:
			switch a.stage {
			case alloc.StagePending:
				s := o.StartedAt
				if s.IsZero() || s.After(now) {
					s = now
				}
				if s.Before(a.at) {
					s = a.at
				}
				a.stage, a.startedAt = alloc.StageRunning, s
				if !lim.Endless {
					a.endsAt = s.Add(a.forD)
					if !o.EndsAt.IsZero() && o.EndsAt.Before(a.endsAt) {
						a.endsAt = o.EndsAt
						if a.endsAt.Before(s) {
							a.endsAt = s
						}
					}
				}
			case alloc.StageRunning, alloc.StageDraining:
				if !a.endsAt.IsZero() && !o.EndsAt.IsZero() && o.EndsAt.Before(a.endsAt) {
					a.endsAt = o.EndsAt
					if a.endsAt.Before(a.startedAt) {
						a.endsAt = a.startedAt
					}
				}
			case alloc.StageEnded:
			}
		case alloc.SeenEnded:
			if a.stage != alloc.StageEnded {
				md.endIt(a)
			}
		}
	case alloc.ProcessesLeft:
		if a, ok := md.m[e.ID]; ok && md.live(a) {
			md.endIt(a)
		}
	case alloc.Unreachable, alloc.Released, alloc.Tick:
	}
	return nil
}

func endsBefore(a, b time.Time) bool {
	switch {
	case a.IsZero():
		return false
	case b.IsZero():
		return true
	}
	return a.Before(b)
}

func (md *model) settle(now time.Time) {
	for _, a := range md.sorted() {
		if !md.live(a) {
			continue
		}
		for a.current == nil && len(a.queue) > 0 {
			next := a.queue[0]
			a.queue = a.queue[1:]
			if md.fits(a, 0, next.bound, now) {
				a.current = &mJob{id: next.id, bound: next.bound}
			}
		}
		if a.stage == alloc.StageDraining && a.current == nil {
			md.endIt(a)
		}
	}
	for _, a := range md.sorted() {
		o := a.renewal
		if a.stage != alloc.StageRunning || o == nil || a.renewedBy != "" || a.endsAt.IsZero() || a.endsAt.Sub(now) > o.Lead {
			continue
		}
		if u, ok := o.Until.(alloc.UntilTime); ok && !now.Before(u.At) {
			continue
		}
		lim, ok := md.cfg.Placements[a.placement]
		if !ok || o.For > lim.MaxFor || md.held(a.run, a.placement) >= lim.MaxHeld {
			continue
		}
		copied := *o
		a.renewedBy = md.create(a.run, a.placement, o.For, now, &copied)
	}
}

// agree says where the model and the core differ, or nothing.
func (md *model) agree(s alloc.State) error {
	got := s.Allocations()
	if len(got) != len(md.m) {
		return fmt.Errorf("the core holds %d allocations, the model %d", len(got), len(md.m))
	}
	for _, a := range got {
		m, ok := md.m[a.ID]
		if !ok {
			return fmt.Errorf("the core holds %s, the model does not", a.ID)
		}
		if a.Stage() != m.stage || a.RenewedBy != m.renewedBy || a.Run != m.run || a.Placement != m.placement {
			return fmt.Errorf("%s: the core has %s renewed by %q, the model %s renewed by %q", a.ID, a.Stage(), a.RenewedBy, m.stage, m.renewedBy)
		}
		var cur core.JobID
		var queue []core.JobID
		if l, ok := liveOf(a); ok {
			if l.Current != nil {
				cur = l.Current.Job
			}
			for _, q := range l.Queue {
				queue = append(queue, q.Job)
			}
			if !l.EndsAt.Equal(m.endsAt) {
				return fmt.Errorf("%s: the core ends at %s, the model at %s", a.ID, l.EndsAt, m.endsAt)
			}
		}
		var mcur core.JobID
		var mqueue []core.JobID
		if m.current != nil {
			mcur = m.current.id
		}
		for _, q := range m.queue {
			mqueue = append(mqueue, q.id)
		}
		if cur != mcur || !slices.Equal(queue, mqueue) {
			return fmt.Errorf("%s: the core runs %q and queues %v, the model runs %q and queues %v", a.ID, cur, queue, mcur, mqueue)
		}
		if (a.Renewal == nil) != (m.renewal == nil) {
			return fmt.Errorf("%s: the core's order is %+v, the model's %+v", a.ID, a.Renewal, m.renewal)
		}
	}
	return nil
}
