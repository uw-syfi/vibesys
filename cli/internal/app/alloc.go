package app

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/changed"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/localalloc"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// AllocPlacement is a placement that offers standing allocations: its operator config has an
// allocations section.
type AllocPlacement struct {
	Name   string
	Limits alloc.Limits
	// Allocator makes and watches the placement's allocations. Nil means slots of the placement
	// itself: an allocation holds one of its max_jobs slots, taken from this daemon's scheduler
	// (localalloc), which is how a machine, local or over ssh, has allocations. A placement whose
	// capacity is a cluster's batch allocations sets it (the apptainer seam, see AllocationLabel in
	// the docker environment and apptainer.NewAllocator).
	Allocator environment.Allocator
	// Slurm says the placement's allocations are batch jobs of a Slurm cluster, so an allocation
	// carries the batch job's words as its detail.
	Slurm bool
}

// Allocations is what the daemon needs to keep standing allocations. A Config whose Log is nil keeps
// none: a submission with an allocation, and every allocation call, fail as unavailable.
type Allocations struct {
	// Log is the daemon's record of its allocations, which it rebuilds them from.
	Log store.AllocLog
	// Placements are the placements that offer allocations. One that is not here offers none.
	Placements []AllocPlacement
	// Owner is who this daemon is across restarts (store/filestore DaemonID): it is what the
	// allocator of the local placements is made with. Empty is allowed for a test.
	Owner string
	// ObservePeriod is how often the backends are asked about the allocations that have not ended,
	// and the core is ticked. It defaults to DefaultObservePeriod.
	ObservePeriod time.Duration
	// JobBound is the longest a check or a measure job is taken to run when it is submitted into an
	// allocation: its plan is made only when it launches, so its own limits are not known before.
	// It defaults to DefaultCommandTimeout. A command job's bound is its own timeout.
	JobBound time.Duration
}

// DefaultObservePeriod is how often allocations are looked at when the config does not say.
const DefaultObservePeriod = 5 * time.Second

// refOfJob is the job the allocation machine names with the ID, and false for an ID that is not a
// job ref (store id, a dash, job id) the daemon makes.
func refOfJob(id core.JobID) (lifecycle.Ref, bool) {
	store, job, ok := strings.Cut(string(id), "-")
	if !ok || store == "" || job == "" {
		return lifecycle.Ref{}, false
	}
	return lifecycle.Ref{Store: lifecycle.StoreID(store), Job: core.JobID(job)}, true
}

// jobOfRef is the ID the allocation machine knows a job by: a job number is a store's own, so the
// store's id is part of it.
func jobOfRef(r lifecycle.Ref) core.JobID { return core.JobID(r.String()) }

// allocMsg is one input of the allocation loop: an event for the core, or a fact about the shell.
type allocMsg struct {
	event alloc.Event
	// reply, when set, receives what came of the event.
	reply chan allocReply
	// done names an effect that finished, so the loop starts it again when it is wanted.
	done string
	// tick is time passing.
	tick bool
	// observeAll asks for a look at every allocation that has not ended.
	observeAll bool
	// adopted is the answer of a placement's backend to Adopt.
	adopted *adoption
	// cancelled is a job whose cancel went through.
	cancelled *lifecycle.Ref
	// cancelAsk is a job that is to be cancelled, with the reason.
	cancelAsk *cancelAsk
}

type cancelAsk struct {
	ref    lifecycle.Ref
	reason string
}

type adoption struct {
	placement string
	handles   []alloc.Handle
}

// allocReply is what the loop answers a request: the allocation it concerns, as it is after the
// event, or why it was refused.
type allocReply struct {
	allocation alloc.Allocation
	run        []alloc.Allocation
	refusal    alloc.Refusal
	err        error
}

// allocSnapshot is the allocations as the loop last published them.
type allocSnapshot struct {
	state alloc.State
}

// allocShell is the thin shell around the allocation machine (internal/alloc): it keeps the log,
// carries out what Step asks, and feeds it what happens to jobs and to the backends. One goroutine,
// the loop, owns the machine's state; everything else posts to it through the feed, which never
// blocks, so the job loop can always feed it.
type allocShell struct {
	app      *App
	cfg      alloc.Config
	conf     Allocations
	backends map[string]environment.Allocator
	slurm    map[string]bool
	slots    bool // a placement uses slots

	mu    sync.Mutex
	queue []allocMsg
	wake  changed.Signal
	// changes wakes whoever waits for an allocation to change.
	changes  changed.Signal
	snapshot atomic.Pointer[allocSnapshot]
	// expected is the jobs a submission has stored and not yet handed to the loop.
	expected map[lifecycle.Ref]int

	// Owned by the loop.
	state      alloc.State
	inflight   map[string]bool
	again      map[string]func(context.Context)
	cancels    map[lifecycle.Ref]string
	waits      map[string][]alloc.WaitSample
	reconciled map[string]bool
	retry      []allocMsg
}

const maxWaitSamples = 64

func newAllocShell(a *App, c Allocations) *allocShell {
	s := &allocShell{
		app: a, conf: c, backends: map[string]environment.Allocator{}, slurm: map[string]bool{},
		expected: map[lifecycle.Ref]int{}, inflight: map[string]bool{}, again: map[string]func(context.Context){}, cancels: map[lifecycle.Ref]string{},
		waits: map[string][]alloc.WaitSample{}, reconciled: map[string]bool{},
	}
	s.cfg = alloc.Config{Placements: map[string]alloc.Limits{}}
	for _, p := range c.Placements {
		s.cfg.Placements[p.Name] = p.Limits
		s.slurm[p.Name] = p.Slurm
		if p.Allocator == nil {
			s.slots = true
			s.backends[p.Name] = localalloc.Allocator{Slots: appSlots{a}, Owner: c.Owner}
		} else {
			s.backends[p.Name] = p.Allocator
		}
	}
	s.snapshot.Store(&allocSnapshot{})
	return s
}

func (s *allocShell) period() time.Duration {
	if s.conf.ObservePeriod > 0 {
		return s.conf.ObservePeriod
	}
	return DefaultObservePeriod
}

// post adds a message to the feed. It never blocks.
func (s *allocShell) post(m allocMsg) {
	s.mu.Lock()
	s.queue = append(s.queue, m)
	s.mu.Unlock()
	s.wake.Notify()
}

func (s *allocShell) take() []allocMsg {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := s.queue
	s.queue = nil
	return out
}

// call gives the loop an event and returns what it answers.
func (s *allocShell) call(ctx context.Context, e alloc.Event) (allocReply, error) {
	reply := make(chan allocReply, 1)
	s.post(allocMsg{event: e, reply: reply})
	select {
	case r := <-reply:
		if r.err != nil {
			return r, r.err
		}
		return r, nil
	case <-ctx.Done():
		return allocReply{}, ctx.Err()
	case <-s.app.ctx.Done():
		return allocReply{}, s.app.ctx.Err()
	}
}

// expect says a stored job is about to be handed to the loop, so no look at the jobs mistakes it
// for one the loop never heard of. Each expect is met by one met.
func (s *allocShell) expect(ref lifecycle.Ref) {
	s.mu.Lock()
	s.expected[ref]++
	s.mu.Unlock()
}

func (s *allocShell) met(ref lifecycle.Ref) {
	s.mu.Lock()
	if s.expected[ref]--; s.expected[ref] <= 0 {
		delete(s.expected, ref)
	}
	s.mu.Unlock()
}

func (s *allocShell) isExpected(ref lifecycle.Ref) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.expected[ref] > 0
}

// noteJob feeds the machine what the job loop learned about a job.
func (s *allocShell) noteJob(e lifecycle.Event) {
	switch e := e.(type) {
	case lifecycle.Launched:
		s.post(allocMsg{event: alloc.JobStarted{Job: jobOfRef(e.Ref)}})
	case lifecycle.Closed:
		s.post(allocMsg{event: alloc.JobFinished{Job: jobOfRef(e.Ref)}})
	case lifecycle.Discarded:
		s.post(allocMsg{event: alloc.JobFinished{Job: jobOfRef(e.Ref)}})
	default:
		// Nothing else about a job is the allocation machine's.
	}
}

// poke asks for a look at the allocations that have not ended, such as when a slot was granted.
func (s *allocShell) poke() { s.post(allocMsg{observeAll: true}) }

// start rebuilds the allocations from the log, restores the slots they hold, and starts the loop.
// It fails only when the log cannot be read.
func (s *allocShell) start(ctx context.Context) error {
	var inputs []alloc.Input
	err := s.app.retry(ctx, func(ctx context.Context) error {
		var err error
		inputs, err = s.conf.Log.Load(ctx)
		return err
	})
	if err != nil {
		return fmt.Errorf("reading the allocation log: %w", err)
	}
	state, actions := alloc.Fold(s.cfg, inputs)
	s.state = state
	for _, a := range actions {
		if w, ok := a.(alloc.RecordWait); ok {
			s.recordWait(w)
		}
	}
	s.publish()
	// A slot an allocation held before the restart is held again at once, before any job takes it.
	for _, al := range state.Allocations() {
		if al.Stage() == alloc.StageEnded || al.Handle == (alloc.Handle{}) || s.usesBackend(al.Placement) {
			continue
		}
		running := al.Stage() == alloc.StageRunning || al.Stage() == alloc.StageDraining
		err := s.app.settle(message{event: lifecycle.SlotWanted{
			Slot: string(al.ID), Placement: al.Placement, Run: al.Run, At: al.RequestedAt, Restored: running,
		}})
		if err != nil {
			return err
		}
	}
	s.app.group.Go(func() { s.loop(named(s.app.ctx, "allocations")) })
	s.app.group.Go(func() { s.clockTicks(named(s.app.ctx, "allocation clock")) })
	return nil
}

// usesBackend says the placement has an allocator of its own, and not slots.
func (s *allocShell) usesBackend(placement string) bool {
	for _, p := range s.conf.Placements {
		if p.Name == placement {
			return p.Allocator != nil
		}
	}
	return false
}

func (s *allocShell) recordWait(w alloc.RecordWait) {
	list := append([]alloc.WaitSample{w.Sample}, s.waits[w.Placement]...)
	s.waits[w.Placement] = list[:min(len(list), maxWaitSamples)]
}

// publish makes the state readable, and wakes whoever waits for a change.
func (s *allocShell) publish() {
	s.snapshot.Store(&allocSnapshot{state: s.state})
	s.changes.Notify()
}

// loop is the only place the machine steps.
func (s *allocShell) loop(ctx context.Context) {
	// The first look is at once: adopting what the backends hold, and observing the records.
	s.tick(ctx)
	for {
		wake := s.wake.Wait()
		for _, m := range s.take() {
			if m.tick {
				s.tick(ctx)
				continue
			}
			s.apply(ctx, m)
		}
		select {
		case <-wake:
		case <-ctx.Done():
			return
		}
	}
}

// clockTicks posts a tick every period.
func (s *allocShell) clockTicks(ctx context.Context) {
	for s.app.sleep(ctx, s.period()) {
		s.post(allocMsg{tick: true})
	}
}

// tick is time passing: the core is ticked, every allocation is looked at, a backend not adopted
// yet is asked, and the jobs the machine and the core may disagree about are looked at.
func (s *allocShell) tick(ctx context.Context) {
	for _, m := range s.retry {
		s.apply(ctx, m)
	}
	s.retry = nil
	s.apply(ctx, allocMsg{event: alloc.Tick{}})
	s.observeAll(ctx)
	for _, p := range s.conf.Placements {
		if !s.reconciled[p.Name] {
			s.adopt(ctx, p.Name)
		}
	}
	s.resendReleases()
	s.cancelPending()
	s.lookAtJobs()
}

// apply steps the machine with one message and carries out what it asks.
func (s *allocShell) apply(ctx context.Context, m allocMsg) {
	if m.done != "" {
		delete(s.inflight, m.done)
		// What was asked for while the effect ran is done once more: it may be about a later state.
		if f, ok := s.again[m.done]; ok {
			delete(s.again, m.done)
			s.effect(ctx, m.done, f)
		}
	}
	if m.cancelled != nil {
		delete(s.cancels, *m.cancelled)
	}
	if m.cancelAsk != nil {
		s.cancels[m.cancelAsk.ref] = m.cancelAsk.reason
	}
	if m.observeAll {
		s.observeAll(ctx)
	}
	if m.adopted != nil {
		s.adoptedBy(ctx, *m.adopted)
	}
	if m.event == nil {
		return
	}
	now := s.app.now()
	next, actions := alloc.Step(s.state, s.cfg, now, m.event)
	var refusal alloc.Refusal
	kept := actions[:0:0]
	for _, act := range actions {
		if r, ok := act.(alloc.Refuse); ok {
			refusal = r.Reason
			continue
		}
		kept = append(kept, act)
	}
	if refusal != nil {
		reply(m, allocReply{refusal: refusal})
		return
	}
	if !next.Equal(s.state) {
		// The input is durable before the state moves, so a restart finds the allocations the
		// callers were told of.
		if err := s.conf.Log.Append(ctx, alloc.Input{At: now, Event: m.event}); err != nil {
			if ctx.Err() != nil {
				return
			}
			s.app.cfg.Logf("the allocation log: %v", err)
			if m.reply != nil {
				reply(m, allocReply{err: &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("the allocation log could not be written: %v", err)}})
			} else {
				s.retry = append(s.retry, m)
			}
			return
		}
		s.state = next
	}
	// What the core asked is started before the change is visible, so whoever sees the change finds
	// the work under way.
	s.perform(ctx, kept)
	s.publish()
	if m.reply != nil {
		r := allocReply{}
		all := s.state.Allocations()
		switch e := m.event.(type) {
		case alloc.Ask:
			if len(all) > 0 {
				r.allocation = all[len(all)-1]
			}
		case alloc.EndAsk:
			r.allocation, _ = s.state.Get(e.ID)
		case alloc.RenewAsk:
			r.allocation, _ = s.state.Get(e.ID)
		case alloc.RenewOffAsk:
			r.allocation, _ = s.state.Get(e.ID)
		case alloc.RunEnded:
			for _, al := range all {
				if al.Run == e.Run {
					r.run = append(r.run, al)
				}
			}
		default:
			// The rest has no allocation to answer with.
		}
		reply(m, r)
	}
}

func reply(m allocMsg, r allocReply) {
	if m.reply != nil {
		m.reply <- r
	}
}

// perform carries out what the core asked.
func (s *allocShell) perform(ctx context.Context, actions []alloc.Action) {
	for _, act := range actions {
		switch act := act.(type) {
		case alloc.Submit:
			s.submit(ctx, act.Spec)
		case alloc.Release:
			s.release(ctx, act)
		case alloc.StartJob:
			s.startJob(act)
		case alloc.LoseJob:
			s.cancel(act.Job, fmt.Sprintf("allocation %s ended (%s) while the job ran in it", act.ID, act.Reason))
		case alloc.DropJob:
			s.cancel(act.Job, act.Reason.Message())
		case alloc.NotPlaced:
			s.placeAsPlain(act.Job)
		case alloc.RecordWait:
			s.recordWait(act)
		case alloc.Refuse:
			// Taken out by apply.
		}
	}
}

// effect starts f beside the loop, one for a key at a time: a request that comes while one runs is
// carried out when it is done, once, however many came.
func (s *allocShell) effect(ctx context.Context, key string, f func(ctx context.Context)) {
	if s.inflight[key] {
		s.again[key] = f
		return
	}
	s.inflight[key] = true
	s.app.group.Go(func() {
		f(ctx)
		if ctx.Err() == nil {
			s.post(allocMsg{done: key})
		}
	})
}

// unreachable is an error that says the backend could not be asked, as opposed to one that says it
// refused for good.
func unreachable(err error) bool {
	var f *core.Failure
	if !errors.As(err, &f) {
		return true
	}
	return f.Kind == core.FailureUnavailable || f.Kind == core.FailureInternal
}

func (s *allocShell) submit(ctx context.Context, spec alloc.Spec) {
	b := s.backends[spec.Placement]
	s.effect(ctx, "submit "+string(spec.ID), func(ctx context.Context) {
		h, err := b.Request(ctx, spec)
		switch {
		case ctx.Err() != nil:
		case err == nil:
			s.post(allocMsg{event: alloc.Submitted{ID: spec.ID, Handle: h}})
			s.post(allocMsg{observeAll: true})
		case unreachable(err):
			s.post(allocMsg{event: alloc.Unreachable{ID: spec.ID, Detail: err.Error()}})
		default:
			s.post(allocMsg{event: alloc.SubmitRefused{ID: spec.ID, Detail: err.Error()}})
		}
	})
}

func (s *allocShell) release(ctx context.Context, r alloc.Release) {
	al, ok := s.state.Get(r.ID)
	if !ok {
		return
	}
	b := s.backends[al.Placement]
	s.effect(ctx, "release "+string(r.ID), func(ctx context.Context) {
		err := b.Release(ctx, r.Handle)
		switch {
		case ctx.Err() != nil:
		case err == nil:
			s.post(allocMsg{event: alloc.Released{ID: r.ID}})
		default:
			s.post(allocMsg{event: alloc.Unreachable{ID: r.ID, Detail: err.Error()}})
		}
	})
}

// startJob lets a job go into the allocation: the job machine launches it there, holding nothing
// of its own, since the allocation holds the capacity.
func (s *allocShell) startJob(act alloc.StartJob) {
	al, ok := s.state.Get(act.ID)
	ref, isRef := refOfJob(act.Job)
	if !ok || !isRef {
		return
	}
	s.releaseJob(ref, al)
}

func (s *allocShell) releaseJob(ref lifecycle.Ref, al alloc.Allocation) {
	e := lifecycle.AllocReleased{Ref: ref, Placement: al.Placement, Allocation: al.Handle.Ref}
	s.app.group.Go(func() { s.app.post(message{event: e}) })
}

// placeAsPlain lets a job that no allocation took run as a job with no allocation does.
func (s *allocShell) placeAsPlain(job core.JobID) {
	ref, ok := refOfJob(job)
	if !ok {
		return
	}
	s.app.group.Go(func() { s.app.post(message{event: lifecycle.AllocReleased{Ref: ref}}) })
}

// resendReleases tells the job machine again where each job an allocation runs is, so that a job
// recovered after a restart finds its allocation.
func (s *allocShell) resendReleases() {
	for _, al := range s.state.Allocations() {
		l, ok := liveOf(al)
		if !ok || l.Current == nil {
			continue
		}
		if ref, ok := refOfJob(l.Current.Job); ok {
			s.releaseJob(ref, al)
		}
	}
}

// liveOf is the jobs a running or draining allocation holds.
func liveOf(a alloc.Allocation) (alloc.Live, bool) {
	switch p := a.Phase.(type) {
	case alloc.Running:
		return p.Live, true
	case alloc.Draining:
		return p.Live, true
	case alloc.Pending, alloc.Ended:
	}
	return alloc.Live{}, false
}

// cancel ends a job for good, and keeps trying until it is final: the allocation machine will not
// say it twice.
func (s *allocShell) cancel(job core.JobID, reason string) {
	if ref, ok := refOfJob(job); ok {
		s.cancels[ref] = reason
	}
}

func (s *allocShell) cancelPending() {
	for ref, reason := range s.cancels {
		s.cancelJob(ref, reason)
	}
}

func (s *allocShell) cancelJob(ref lifecycle.Ref, reason string) {
	ctx := s.app.ctx
	s.effect(named(ctx, "cancel "+ref.String()), "cancel "+ref.String(), func(ctx context.Context) {
		scope, ok := s.app.scopeOfStore(ctx, ref.Store)
		if !ok {
			return
		}
		if _, err := s.app.Cancel(ctx, core.CallContext{Scope: scope}, ref.Job, reason); err != nil {
			if ctx.Err() == nil {
				s.app.cfg.Logf("cancelling job %s: %v", ref, err)
			}
			return
		}
		if ctx.Err() == nil {
			s.post(allocMsg{cancelled: &ref})
		}
	})
}

// observeAll asks each backend about every allocation that has not ended.
func (s *allocShell) observeAll(ctx context.Context) {
	for _, al := range s.state.Allocations() {
		if al.Stage() == alloc.StageEnded || al.Handle == (alloc.Handle{}) {
			continue
		}
		b := s.backends[al.Placement]
		id, h := al.ID, al.Handle
		s.effect(ctx, "observe "+string(id), func(ctx context.Context) {
			seen, err := b.Observe(ctx, h)
			switch {
			case ctx.Err() != nil:
			case err != nil:
				s.post(allocMsg{event: alloc.Unreachable{ID: id, Detail: err.Error()}})
			default:
				s.post(allocMsg{event: alloc.Observed{ID: id, Seen: seen}})
			}
		})
	}
}

// adopt asks a placement's backend for the allocations it holds that this daemon made.
func (s *allocShell) adopt(ctx context.Context, placement string) {
	b := s.backends[placement]
	s.effect(ctx, "adopt "+placement, func(ctx context.Context) {
		handles, err := b.Adopt(ctx)
		if err != nil {
			if ctx.Err() == nil {
				s.app.cfg.Logf("adopting the allocations of placement %s: %v", placement, err)
			}
			return
		}
		s.post(allocMsg{adopted: &adoption{placement: placement, handles: handles}})
	})
}

// adoptedBy reconciles the records with what a backend holds: an allocation it holds that no record
// names is released, and one a record asked for that the backend took, whose answer was lost, is
// recorded as taken. One a record has and the backend lacks is seen ended by the next look.
func (s *allocShell) adoptedBy(ctx context.Context, found adoption) {
	s.reconciled[found.placement] = true
	b := s.backends[found.placement]
	for _, h := range found.handles {
		al, ok := s.state.Get(h.Alloc)
		switch {
		case !ok || al.Stage() == alloc.StageEnded && al.Handle == (alloc.Handle{}):
			s.effect(ctx, "release unknown "+string(h.Alloc), func(ctx context.Context) {
				if err := b.Release(ctx, h); err != nil && ctx.Err() == nil {
					s.app.cfg.Logf("releasing allocation %s, which no record names: %v", h.Alloc, err)
				}
			})
		case al.Handle == (alloc.Handle{}) && al.Stage() == alloc.StagePending:
			s.apply(ctx, allocMsg{event: alloc.Submitted{ID: h.Alloc, Handle: h}})
			s.observeAll(ctx)
		default:
			// A record that has its handle.
		}
	}
}

// lookAtJobs settles the jobs the machine and the allocation machine may disagree about after a
// crash between them: a job held back for an allocation that no allocation holds, and a job an
// allocation holds that the machine does not know and the store shows done.
func (s *allocShell) lookAtJobs() {
	v := s.app.view.Load()
	held := map[core.JobID]bool{}
	for _, al := range s.state.Allocations() {
		if l, ok := liveOf(al); ok {
			if l.Current != nil {
				held[l.Current.Job] = true
			}
			for _, q := range l.Queue {
				held[q.Job] = true
			}
		}
	}
	var unplaced, lost []lifecycle.Ref
	for _, ref := range v.unplaced {
		if !held[jobOfRef(ref)] && !s.isExpected(ref) {
			unplaced = append(unplaced, ref)
		}
	}
	for id := range held {
		if ref, ok := refOfJob(id); ok && !v.held[ref] && !s.isExpected(ref) {
			lost = append(lost, ref)
		}
	}
	if len(unplaced) == 0 && len(lost) == 0 {
		return
	}
	slices.SortFunc(unplaced, func(a, b lifecycle.Ref) int { return strings.Compare(a.String(), b.String()) })
	slices.SortFunc(lost, func(a, b lifecycle.Ref) int { return strings.Compare(a.String(), b.String()) })
	ctx := s.app.ctx
	s.effect(named(ctx, "allocation jobs"), "jobs", func(ctx context.Context) {
		for _, ref := range unplaced {
			s.settleUnplaced(ctx, ref)
		}
		for _, ref := range lost {
			s.settleLost(ctx, ref)
		}
	})
}

// settleUnplaced decides a job no allocation holds: one submitted to any allocation runs as a job
// with no allocation does, and one submitted into an allocation by name is cancelled, since that
// allocation never took it.
func (s *allocShell) settleUnplaced(ctx context.Context, ref lifecycle.Ref) {
	job, ok := s.jobOf(ctx, ref)
	if !ok || job.State != core.JobQueued {
		return
	}
	if job.Spec.Allocation == core.AllocationAny {
		s.app.post(message{event: lifecycle.AllocReleased{Ref: ref}})
		return
	}
	s.post(allocMsg{cancelAsk: &cancelAsk{ref: ref, reason: fmt.Sprintf("allocation %s never took the job", job.Spec.Allocation)}})
}

// settleLost tells the allocation machine a job it holds is done, when its store says so.
func (s *allocShell) settleLost(ctx context.Context, ref lifecycle.Ref) {
	scope, ok := s.app.scopeOfStore(ctx, ref.Store)
	if !ok {
		return
	}
	st, err := s.app.cfg.Stores.Find(ctx, scope)
	if err != nil {
		return
	}
	job, err := st.Job(ctx, ref.Job)
	if err != nil {
		return
	}
	if job.State.Final() {
		s.post(allocMsg{event: alloc.JobFinished{Job: jobOfRef(ref)}})
	}
}

// jobOf reads a job from its store.
func (s *allocShell) jobOf(ctx context.Context, ref lifecycle.Ref) (core.Job, bool) {
	scope, ok := s.app.scopeOfStore(ctx, ref.Store)
	if !ok {
		return core.Job{}, false
	}
	st, err := s.app.cfg.Stores.Find(ctx, scope)
	if err != nil {
		return core.Job{}, false
	}
	job, err := st.Job(ctx, ref.Job)
	return job, err == nil
}

// scopeOfStore finds the scope of a store the daemon knows.
func (a *App) scopeOfStore(ctx context.Context, id lifecycle.StoreID) (core.Scope, bool) {
	scopes, err := a.cfg.Stores.Known(ctx)
	if err != nil {
		return core.Scope{}, false
	}
	for _, s := range scopes {
		if storeIDOf(s) == id {
			return s, true
		}
	}
	return core.Scope{}, false
}

// appSlots is the capacity of the local placements, as localalloc takes it: the job machine's
// slots, which it grants as it grants a job its own.
type appSlots struct{ a *App }

var _ localalloc.Slots = appSlots{}

func (s appSlots) Want(ctx context.Context, spec alloc.Spec) error {
	return s.a.settleEffect(ctx, message{event: lifecycle.SlotWanted{
		Slot: string(spec.ID), Placement: spec.Placement, Run: spec.Run, At: s.a.now(),
	}})
}

func (s appSlots) State(_ context.Context, id alloc.ID) (localalloc.SlotState, error) {
	granted, ok := s.a.view.Load().slots[string(id)]
	switch {
	case !ok:
		return localalloc.SlotNone, nil
	case granted:
		return localalloc.SlotHeld, nil
	}
	return localalloc.SlotWaiting, nil
}

func (s appSlots) Drop(ctx context.Context, id alloc.ID) error {
	return s.a.settleEffect(ctx, message{event: lifecycle.SlotDropped{Slot: string(id)}})
}

func (s appSlots) Wanted(_ context.Context) ([]alloc.ID, error) {
	var out []alloc.ID
	for slot := range s.a.view.Load().slots {
		out = append(out, alloc.ID(slot))
	}
	slices.Sort(out)
	return out, nil
}
