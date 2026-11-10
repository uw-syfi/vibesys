package alloc

import (
	"reflect"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// State is every standing allocation the daemon has made, of every run, and the runs that ended.
// The zero State holds none. A State is a value: Step never changes the one it is given.
type State struct {
	allocs    []Allocation // In the order they were made, which is the order of their numbers.
	last      uint64       // The number of the newest allocation.
	endedRuns []core.RunID // Sorted.
}

// Allocations returns a copy of every allocation, oldest first.
func (s State) Allocations() []Allocation {
	out := make([]Allocation, len(s.allocs))
	for i, a := range s.allocs {
		out[i] = a.clone()
	}
	return out
}

// Get returns the allocation the ID names, and false when there is none.
func (s State) Get(id ID) (Allocation, bool) {
	if i := s.index(id); i >= 0 {
		return s.allocs[i].clone(), true
	}
	return Allocation{}, false
}

// Held is how many allocations of the placement the run holds: those that have not ended.
func (s State) Held(run core.RunID, placement string) int {
	n := 0
	for _, a := range s.allocs {
		if a.Run == run && a.Placement == placement && a.holds() {
			n++
		}
	}
	return n
}

// RunEnded says whether the run has ended.
func (s State) RunEnded(run core.RunID) bool {
	_, ok := slices.BinarySearch(s.endedRuns, run)
	return ok
}

// Equal says whether two states hold the same allocations, numbering and ended runs. The shell asks
// it of the state before a step and after, to keep only the inputs that changed something.
func (s State) Equal(o State) bool {
	return s.last == o.last && slices.Equal(s.endedRuns, o.endedRuns) && reflect.DeepEqual(s.Allocations(), o.Allocations())
}

// Durable says whether a step from before to after changed what the log keeps: anything but the
// words a backend gives a pending allocation for display (Pending.Reason and EstimatedStart), which
// a backend such as Slurm may change at every look. The shell logs a step only when it is durable,
// and keeps the display words in memory, so a restart shows them again at the next look instead of
// replaying every change of an estimate.
func Durable(before, after State) bool {
	return !before.withoutDisplay().Equal(after.withoutDisplay())
}

// withoutDisplay is the state with the display words of every pending allocation cleared.
func (s State) withoutDisplay() State {
	out := s.clone()
	for i, a := range out.allocs {
		if _, ok := a.Phase.(Pending); ok {
			out.allocs[i].Phase = Pending{}
		}
	}
	return out
}

func (s State) index(id ID) int {
	return slices.IndexFunc(s.allocs, func(a Allocation) bool { return a.ID == id })
}

func (s State) clone() State {
	out := State{allocs: make([]Allocation, len(s.allocs)), last: s.last, endedRuns: slices.Clone(s.endedRuns)}
	for i, a := range s.allocs {
		out.allocs[i] = a.clone()
	}
	return out
}

// Input is one event and when it happened, as a log of them is kept for Fold.
type Input struct {
	At    time.Time
	Event Event
}

// Fold runs Step over the inputs from the zero State, and returns the state and every action, in
// order. The same inputs give the same state and actions, which is how the daemon recovers. Each
// input is stepped under the config of the last DaemonStarted before it, and the inputs before the
// first one under cfg, so a log that records its configs folds the same whatever cfg is.
func Fold(cfg Config, inputs []Input) (State, []Action) {
	var s State
	var all []Action
	for _, in := range inputs {
		if started, ok := in.Event.(DaemonStarted); ok {
			cfg = started.Config
		}
		var actions []Action
		s, actions = Step(s, cfg, in.At, in.Event)
		all = append(all, actions...)
	}
	return s, all
}

// Step applies one event at now and returns the new state and what the shell must do. A refused
// request returns s as it was and one Refuse. Otherwise, after the event, every allocation is
// settled at now: a free Running or Draining allocation starts the next job of its queue that
// fits, a Draining one with nothing left ends, and a renewal order that is due asks for its
// successor or says why not.
func Step(s State, cfg Config, now time.Time, e Event) (State, []Action) {
	out := s.clone()
	st := &stepper{s: &out, cfg: cfg, now: now}
	var refusal Refusal
	switch e := e.(type) {
	case Ask:
		refusal = st.ask(e)
	case EndAsk:
		refusal = st.endAsk(e)
	case RenewAsk:
		refusal = st.renewAsk(e)
	case RenewOffAsk:
		refusal = st.renewOff(e)
	case JobSubmitted:
		refusal = st.submitted(e)
	case RunEnded:
		st.runEnded(e.Run)
	case JobStarted:
		st.jobStarted(e.Job)
	case JobFinished:
		st.jobFinished(e.Job)
	case Submitted:
		st.backendTook(e)
	case SubmitRefused:
		// Only a submission the backend never took is refused for good; a later Submit asked again of
		// one it took is the same allocation, which the backend still holds.
		if a := st.find(e.ID); a != nil && a.Stage() == StagePending && a.Handle == (Handle{}) {
			a.Phase = Ended{At: now, Reason: EndExternal, Released: true}
		}
	case Observed:
		st.observed(e)
	case Unreachable:
		// Never an end, nor anything else: the allocation is as it was last seen.
	case Released:
		if a := st.find(e.ID); a != nil {
			if p, ok := a.Phase.(Ended); ok {
				p.Released = true
				a.Phase = p
			}
		}
	case Tick:
		st.retry()
	case DaemonStarted:
		st.cfg = e.Config
		for i := range st.s.allocs {
			a := &st.s.allocs[i]
			if _, enabled := e.Config.Placements[a.Placement]; !enabled && a.holds() {
				st.end(a, EndExternal)
			}
		}
	}
	if refusal != nil {
		return s, []Action{Refuse{Event: e, Reason: refusal}}
	}
	st.settle()
	return out, st.actions
}

// stepper is one Step under way.
type stepper struct {
	s       *State
	cfg     Config
	now     time.Time
	actions []Action
}

func (st *stepper) emit(a Action) { st.actions = append(st.actions, a) }

func (st *stepper) find(id ID) *Allocation {
	if i := st.s.index(id); i >= 0 {
		return &st.s.allocs[i]
	}
	return nil
}

// owned is the run's allocation the ID names, or why the run may not touch it.
func (st *stepper) owned(run core.RunID, id ID) (*Allocation, Refusal) {
	a := st.find(id)
	switch {
	case a == nil:
		return nil, UnknownAllocation{ID: id}
	case a.Run != run:
		return nil, NotOwnedByRun{ID: id, Owner: a.Run, Run: run}
	}
	return a, nil
}

// checkFor refuses a time limit the placement does not allow.
func checkFor(placement string, d time.Duration, lim Limits) Refusal {
	switch {
	case d <= 0:
		return ForNotPositive{For: d}
	case d > lim.MaxFor:
		return ForExceedsMaxFor{For: d, MaxFor: lim.MaxFor, Key: ConfigKey(placement) + ".max_for"}
	}
	return nil
}

func (st *stepper) ask(e Ask) Refusal {
	lim, ok := st.cfg.Placements[e.Placement]
	switch {
	case st.s.RunEnded(e.Run):
		return RunNotLive{Run: e.Run}
	case !ok:
		return NotEnabled{Placement: e.Placement, Key: ConfigKey(e.Placement)}
	}
	if r := checkFor(e.Placement, e.For, lim); r != nil {
		return r
	}
	if held := st.s.Held(e.Run, e.Placement); held >= lim.MaxHeld {
		return MaxHeldReached{Placement: e.Placement, Held: held, MaxHeld: lim.MaxHeld}
	}
	st.add(e.Run, e.Placement, e.For, nil)
	return nil
}

// add adds a new Pending allocation and asks the backend for it.
func (st *stepper) add(run core.RunID, placement string, d time.Duration, order *RenewalOrder) ID {
	st.s.last++
	id := idOf(st.s.last)
	a := Allocation{ID: id, Placement: placement, Run: run, RequestedFor: d, RequestedAt: st.now, Phase: Pending{}}
	if order != nil {
		o := *order
		a.Renewal = &o
	}
	st.s.allocs = append(st.s.allocs, a)
	st.emit(Submit{Spec: Spec{ID: id, Run: run, Placement: placement, For: d}})
	return id
}

func (st *stepper) endAsk(e EndAsk) Refusal {
	a, r := st.owned(e.Run, e.ID)
	if r != nil {
		return r
	}
	switch p := a.Phase.(type) {
	case Running:
		if e.AfterJobs && (p.Current != nil || len(p.Queue) > 0) {
			a.Phase = Draining(p)
			return nil
		}
		st.end(a, EndRequested)
	case Draining:
		if !e.AfterJobs {
			st.end(a, EndRequested)
		}
	case Pending:
		st.end(a, EndRequested)
	case Ended:
	}
	return nil
}

func (st *stepper) renewAsk(e RenewAsk) Refusal {
	a, r := st.owned(e.Run, e.ID)
	if r != nil {
		return r
	}
	lim, enabled := st.cfg.Placements[a.Placement]
	o := e.Order
	switch {
	case st.s.RunEnded(e.Run):
		return RunNotLive{Run: e.Run}
	case a.Stage() != StagePending && a.Stage() != StageRunning:
		return NotRenewable{ID: a.ID, Stage: a.Stage()}
	case a.RenewedBy != "":
		return AlreadyRenewed{ID: a.ID, By: a.RenewedBy}
	case !enabled:
		return NotEnabled{Placement: a.Placement, Key: ConfigKey(a.Placement)}
	case lim.Endless:
		return NoEnd{ID: a.ID}
	case o.Lead <= 0:
		return LeadNotPositive{Lead: o.Lead}
	}
	if r := checkFor(a.Placement, o.For, lim); r != nil {
		return r
	}
	if o.Lead >= o.For {
		return LeadNotShorterThanFor{Lead: o.Lead, For: o.For}
	}
	switch u := o.Until.(type) {
	case UntilTime:
		if !u.At.After(st.now) {
			return UntilPassed{At: u.At, Now: st.now}
		}
	case UntilRunEnd:
	case nil:
		return NoUntil{}
	}
	a.Renewal = &o
	a.RenewalSkipped = ""
	return nil
}

func (st *stepper) renewOff(e RenewOffAsk) Refusal {
	a, r := st.owned(e.Run, e.ID)
	if r != nil {
		return r
	}
	a.Renewal = nil
	a.RenewalSkipped = ""
	return nil
}

func (st *stepper) runEnded(run core.RunID) {
	if !st.s.RunEnded(run) {
		i, _ := slices.BinarySearch(st.s.endedRuns, run)
		st.s.endedRuns = slices.Insert(st.s.endedRuns, i, run)
	}
	for i := range st.s.allocs {
		if a := &st.s.allocs[i]; a.Run == run && a.holds() {
			st.end(a, EndRequested)
		}
	}
}

// holder is the allocation that holds the job, running or queued, and false for none.
func (st *stepper) holder(job core.JobID) (*Allocation, bool) {
	for i := range st.s.allocs {
		a := &st.s.allocs[i]
		l, ok := a.live()
		if !ok {
			continue
		}
		if (l.Current != nil && l.Current.Job == job) || slices.ContainsFunc(l.Queue, func(q Slot) bool { return q.Job == job }) {
			return a, true
		}
	}
	return nil, false
}

func (st *stepper) submitted(e JobSubmitted) Refusal {
	switch {
	case st.s.RunEnded(e.Run):
		return RunNotLive{Run: e.Run}
	case e.Bound <= 0:
		return BoundNotPositive{Job: e.Job, Bound: e.Bound}
	}
	if a, held := st.holder(e.Job); held {
		return DuplicateJob{Job: e.Job, ID: a.ID}
	}
	slot := Slot{Job: e.Job, Bound: e.Bound, Any: e.Into == ""}
	if e.Into == "" {
		id, ok := Choose(*st.s, st.cfg, e, st.now)
		if !ok {
			st.emit(NotPlaced{Job: e.Job, Why: Declined(*st.s, st.cfg, e, st.now)})
			return nil
		}
		st.enqueue(st.find(id), slot)
		return nil
	}
	a, r := st.owned(e.Run, e.Into)
	if r != nil {
		return r
	}
	if !slices.Contains(e.Eligible, a.Placement) {
		return NotEligible{ID: a.ID, Placement: a.Placement}
	}
	p, ok := a.Phase.(Running)
	if !ok {
		return notRunning(*a)
	}
	margin := st.cfg.Placements[a.Placement].Margin
	if f := Fit(p.Live, e.Bound, st.now, margin); !f.Fits {
		return DoesNotFit{ID: a.ID, TimeLeft: f.TimeLeft, Ahead: f.Ahead, Bound: e.Bound, Margin: margin}
	}
	st.enqueue(a, slot)
	return nil
}

func notRunning(a Allocation) NotRunning {
	r := NotRunning{ID: a.ID, Stage: a.Stage()}
	if p, ok := a.Phase.(Ended); ok {
		r.EndReason = p.Reason
	}
	return r
}

func (st *stepper) enqueue(a *Allocation, slot Slot) {
	p := a.Phase.(Running)
	p.Queue = append(p.Queue, slot)
	a.Phase = p
}

func (st *stepper) jobStarted(job core.JobID) {
	a, ok := st.holder(job)
	if !ok {
		return
	}
	st.withLive(a, func(l *Live) {
		if l.Current != nil && l.Current.Job == job && l.Current.StartedAt.IsZero() {
			l.Current.StartedAt = st.now
		}
	})
}

func (st *stepper) jobFinished(job core.JobID) {
	a, ok := st.holder(job)
	if !ok {
		return
	}
	st.withLive(a, func(l *Live) {
		if l.Current != nil && l.Current.Job == job {
			l.Current = nil
			return
		}
		l.Queue = slices.DeleteFunc(l.Queue, func(q Slot) bool { return q.Job == job })
	})
}

// withLive changes the Live part of a Running or Draining allocation in place.
func (st *stepper) withLive(a *Allocation, change func(*Live)) {
	switch p := a.Phase.(type) {
	case Running:
		change(&p.Live)
		a.Phase = p
	case Draining:
		change(&p.Live)
		a.Phase = p
	case Pending, Ended:
	}
}

func (st *stepper) backendTook(e Submitted) {
	a := st.find(e.ID)
	if a == nil || a.Handle != (Handle{}) || e.Handle.Ref == "" || e.Handle.Alloc != e.ID {
		return
	}
	a.Handle = e.Handle
	if p, ok := a.Phase.(Ended); ok && !p.Released {
		st.emit(Release{ID: a.ID, Handle: a.Handle})
	}
}

// end ends an allocation that has not ended, at now: the running job is lost, the queued ones are
// dropped or placed elsewhere, and, for an end the backend did not make, its capacity is released.
func (st *stepper) end(a *Allocation, reason EndReason) {
	st.endAt(a, reason, st.now, false)
}

func (st *stepper) endAt(a *Allocation, reason EndReason, at time.Time, byBackend bool) {
	ended := Ended{At: at, Reason: reason, Released: byBackend}
	switch p := a.Phase.(type) {
	case Ended:
		return
	case Pending:
	case Running:
		ended.StartedAt = p.StartedAt
		st.orphan(a, p.Live, reason)
	case Draining:
		ended.StartedAt = p.StartedAt
		st.orphan(a, p.Live, reason)
	}
	a.Phase = ended
	if !byBackend && a.Handle != (Handle{}) {
		st.emit(Release{ID: a.ID, Handle: a.Handle})
	}
}

// orphan says what becomes of the jobs of an allocation that ends.
func (st *stepper) orphan(a *Allocation, l Live, reason EndReason) {
	if l.Current != nil {
		st.emit(LoseJob{Job: l.Current.Job, ID: a.ID, Reason: reason})
	}
	for _, q := range l.Queue {
		st.unplace(a, q, NotRunning{ID: a.ID, Stage: StageEnded, EndReason: reason})
	}
}

// unplace gives up a job that will not run in the allocation: one of any runs the way a job
// without an allocation does, and a strict one fails.
func (st *stepper) unplace(a *Allocation, q Slot, why Refusal) {
	if q.Any {
		st.emit(NotPlaced{Job: q.Job, Why: why})
		return
	}
	st.emit(DropJob{Job: q.Job, ID: a.ID, Reason: why})
}

func (st *stepper) observed(e Observed) {
	a := st.find(e.ID)
	if a == nil {
		return
	}
	lim := st.cfg.Placements[a.Placement]
	switch seen := e.Seen.(type) {
	case SeenPending:
		p, ok := a.Phase.(Pending)
		if !ok {
			return // The stages only move forward.
		}
		if lim.QueueTimeout > 0 && st.now.Sub(a.RequestedAt) >= lim.QueueTimeout {
			st.end(a, EndQueueTimeout)
			return
		}
		p.Reason, p.EstimatedStart = seen.Reason, seen.EstimatedStart
		a.Phase = p
	case SeenRunning:
		st.seenRunning(a, seen, lim)
	case SeenEnded:
		if _, ok := a.Phase.(Ended); ok {
			st.withEnded(a, func(p *Ended) { p.Released = true })
			return
		}
		reason := EndExternal
		if seen.Cause == EndedByTimeLimit {
			reason = EndTimeLimit
		}
		st.endAt(a, reason, st.clampEnd(*a, seen.At), true)
	case nil:
	}
}

func (st *stepper) withEnded(a *Allocation, change func(*Ended)) {
	if p, ok := a.Phase.(Ended); ok {
		change(&p)
		a.Phase = p
	}
}

// clampEnd is when an allocation ended: the backend's word, kept between its start (or request)
// and now, or now when the backend does not say.
func (st *stepper) clampEnd(a Allocation, at time.Time) time.Time {
	if at.IsZero() || at.After(st.now) {
		return st.now
	}
	floor := a.RequestedAt
	if l, ok := a.live(); ok {
		floor = l.StartedAt
	}
	if at.Before(floor) {
		return floor
	}
	return at
}

func (st *stepper) seenRunning(a *Allocation, seen SeenRunning, lim Limits) {
	switch a.Phase.(type) {
	case Pending:
		started := seen.StartedAt
		if started.IsZero() || started.After(st.now) {
			started = st.now
		}
		if started.Before(a.RequestedAt) {
			started = a.RequestedAt
		}
		live := Live{StartedAt: started, Node: seen.Node}
		if !lim.Endless {
			live.EndsAt = endsAt(started, a.RequestedFor, seen.EndsAt)
		}
		a.Phase = Running{Live: live}
		st.emit(RecordWait{Placement: a.Placement, Sample: WaitSample{RequestedAt: a.RequestedAt, Waited: started.Sub(a.RequestedAt), RequestedFor: a.RequestedFor}})
	case Running, Draining:
		st.withLive(a, func(l *Live) {
			if seen.Node != "" {
				l.Node = seen.Node
			}
			if !l.EndsAt.IsZero() && !seen.EndsAt.IsZero() && seen.EndsAt.Before(l.EndsAt) {
				l.EndsAt = later(seen.EndsAt, l.StartedAt)
			}
		})
	case Ended:
		// A release that has not landed yet: the next Tick asks for it again.
	}
}

// endsAt is when an allocation that started ends: by its time limit, or sooner when the backend
// says so. Never later than the time limit, which the backend enforces.
func endsAt(started time.Time, d time.Duration, said time.Time) time.Time {
	limit := started.Add(d)
	if !said.IsZero() && said.Before(limit) {
		return later(said, started)
	}
	return limit
}

// later is the later of two instants.
func later(x, y time.Time) time.Time {
	if x.Before(y) {
		return y
	}
	return x
}

// retry asks again for what the backend may not have done: a Submit it never took, and a Release
// it never confirmed.
func (st *stepper) retry() {
	for _, a := range st.s.allocs {
		switch p := a.Phase.(type) {
		case Pending:
			if a.Handle == (Handle{}) {
				st.emit(Submit{Spec: Spec{ID: a.ID, Run: a.Run, Placement: a.Placement, For: a.RequestedFor}})
			}
		case Ended:
			if !p.Released && a.Handle != (Handle{}) {
				st.emit(Release{ID: a.ID, Handle: a.Handle})
			}
		case Running, Draining:
		}
	}
}

// settle applies, at now, what follows from the state whatever the event was.
func (st *stepper) settle() {
	for i := range st.s.allocs {
		st.advance(&st.s.allocs[i])
	}
	for i := range st.s.allocs {
		st.renew(i)
	}
}

// advance starts the next job of a free allocation, and ends a draining one with nothing left. A
// queued job that no longer fits is given up, never started.
func (st *stepper) advance(a *Allocation) {
	l, ok := a.live()
	if !ok {
		return
	}
	margin := st.cfg.Placements[a.Placement].Margin
	for l.Current == nil && len(l.Queue) > 0 {
		next := l.Queue[0]
		l.Queue = l.Queue[1:]
		if f := Fit(Live{EndsAt: l.EndsAt}, next.Bound, st.now, margin); !f.Fits {
			st.unplace(a, next, DoesNotFit{ID: a.ID, TimeLeft: f.TimeLeft, Bound: next.Bound, Margin: margin})
			continue
		}
		next.StartedAt = time.Time{}
		l.Current = &next
		st.emit(StartJob{Job: next.Job, ID: a.ID, Bound: next.Bound})
	}
	switch a.Phase.(type) {
	case Running:
		a.Phase = Running{Live: l}
	case Draining:
		a.Phase = Draining{Live: l}
		if l.Current == nil {
			st.end(a, EndRequested)
		}
	case Pending, Ended:
	}
}

// renew applies the renewal rule to one allocation: Running, with an order, no successor yet, and
// time left at most the lead. Then it asks for a successor carrying the order, if the run may hold
// one more, and says why not otherwise.
func (st *stepper) renew(i int) {
	a := &st.s.allocs[i]
	p, ok := a.Phase.(Running)
	if !ok || a.Renewal == nil || a.RenewedBy != "" || p.EndsAt.IsZero() {
		return
	}
	o := *a.Renewal
	if p.EndsAt.Sub(st.now) > o.Lead {
		a.RenewalSkipped = ""
		return
	}
	lim, enabled := st.cfg.Placements[a.Placement]
	var skip SkipReason
	switch u := o.Until.(type) {
	case UntilTime:
		if !st.now.Before(u.At) {
			skip = SkipPastUntil
		}
	case UntilRunEnd, nil:
	}
	switch {
	case skip != "":
	case !enabled:
		skip = SkipNotEnabled
	case o.For > lim.MaxFor:
		skip = SkipForExceedsMaxFor
	case st.s.Held(a.Run, a.Placement) >= lim.MaxHeld:
		skip = SkipMaxHeld
	}
	if skip != "" {
		a.RenewalSkipped = skip
		return
	}
	run, placement := a.Run, a.Placement
	id := st.add(run, placement, o.For, &o)
	a = &st.s.allocs[i] // add appended, which may have moved the slice.
	a.RenewedBy = id
	a.RenewalSkipped = ""
}
