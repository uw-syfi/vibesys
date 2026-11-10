package sim_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"slices"
	"strings"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakealloc"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/store/memstore"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// The simulation of standing allocations: runs ask for allocations of two placements, one whose
// capacity is a cluster's (a fake backend that starts allocations after some looks, ends them at
// their time limit, loses them to someone's cancel, and may be unreachable) and one whose capacity
// is slots of the machine; they submit jobs into them by name and for any, end them, renew them, and
// end; and the daemon dies and restarts through all of it. The driver gates every call to the
// backend and to the allocation log, as it gates the stores'.
//
// What is checked after every step is what the design calls the invariants of the daemon: the
// daemon's allocations are exactly what its log folds to, so a restart finds the same; no more
// allocations than max_held, one successor per renewal; an allocation ends only for a reason that
// happened; a job starts only as the allocation's current job, one at a time, in an allocation
// that takes jobs; and nothing the backend holds is a record's that was released or that no record
// names. When the world heals, every run ends, and nothing is left: no allocation, no job, no
// capacity at the backend.

const (
	placementCluster = "p"
	placementSlots   = "q"
	maxHeld          = 2
	maxFor           = 2 * time.Hour
	fitMargin        = time.Minute
	// allocPeriod is how often the daemon looks at its allocations, which is how fast their time
	// limits are noticed in the simulation.
	allocPeriod = 2 * time.Minute
	// jobBound is the time a command is taken to need in an allocation: its timeout, which the
	// simulation's commands never reach, since its environment has no clock.
	jobBound = 5 * time.Minute
)

// allocationsOf is a profile of runs holding standing allocations through every fault and restart.
func allocationsOf(r *rand.Rand) profile {
	coin := func() bool { return r.IntN(2) == 0 }
	return profile{
		allocs: true, slots: 2 + r.IntN(3), fair: coin(),
		storeWrites: coin(), supervisorFaults: coin(), crashes: r.IntN(3) != 0, machine: coin(),
		attempts: 4 + r.IntN(5), faultPercent: r.IntN(9), chaosSteps: 500 + r.IntN(700), jobs: 10 + r.IntN(10),
	}
}

// allocWorld is what outlives a daemon in a profile of allocations: its log, which is the disk, and
// the cluster, which is the backend.
type allocWorld struct {
	log  *memstore.AllocLog
	fake *fakealloc.FakeAllocator
	cfg  alloc.Config

	mu sync.Mutex
	// endAsked says an end of the allocation was asked for, by name or by its run's end.
	endAsked map[alloc.ID]bool
	// runEnding says the end of the run was asked for, and runEnded that it was answered.
	runEnding, runEnded map[string]bool
	// cancelled are the backend references someone cancelled outside the CLI.
	cancelled map[string]bool
	// ended is the end each allocation had when it was first seen ended, and renewedBy the successor
	// it had when it was first seen to have one.
	stages    map[alloc.ID]alloc.Stage
	ended     map[alloc.ID]alloc.Ended
	renewedBy map[alloc.ID]alloc.ID
	requests  int
	// down says the driver made the cluster unreachable.
	down bool
}

func newAllocWorld(w *world) *allocWorld {
	return &allocWorld{
		log:  &memstore.AllocLog{},
		fake: fakealloc.New(w.clock, fakealloc.Options{Looks: w.rng.IntN(3)}),
		cfg: alloc.Config{Placements: map[string]alloc.Limits{
			placementCluster: {MaxHeld: maxHeld, MaxFor: maxFor, Margin: fitMargin},
			placementSlots:   {MaxHeld: maxHeld, MaxFor: maxFor, Margin: fitMargin, Endless: true},
		}},
		endAsked: map[alloc.ID]bool{}, runEnding: map[string]bool{}, runEnded: map[string]bool{},
		cancelled: map[string]bool{}, stages: map[alloc.ID]alloc.Stage{}, ended: map[alloc.ID]alloc.Ended{}, renewedBy: map[alloc.ID]alloc.ID{},
	}
}

// state is the allocations the log folds to.
func (a *allocWorld) state() alloc.State {
	inputs, err := a.log.Load(context.Background())
	if err != nil {
		panic(err)
	}
	s, _ := alloc.Fold(a.cfg, inputs)
	return s
}

// allocations is how a daemon of the world keeps allocations: over the log and the cluster, with
// every call gated.
func (w *world) allocations(d *incarnation) app.Allocations {
	if w.alloc == nil {
		return app.Allocations{}
	}
	a := w.alloc
	return app.Allocations{
		Log: gatedAllocLog{w: w, d: d, inner: a.log},
		Placements: []app.AllocPlacement{
			{Name: placementCluster, Limits: a.cfg.Placements[placementCluster], Allocator: gatedAllocator{w: w, d: d, inner: a.fake}},
			{Name: placementSlots, Limits: a.cfg.Placements[placementSlots]},
		},
		ObservePeriod: allocPeriod,
	}
}

// allocDemand lets a job run on either placement, claiming one of its slots.
type allocDemand struct{}

func (allocDemand) Options(core.Job) []schedule.Option {
	var out []schedule.Option
	for _, p := range []string{placementCluster, placementSlots} {
		out = append(out, schedule.Option{Placement: p, Claims: []schedule.Claim{{Resource: schedule.PlacementOf(p), Amount: 1}}})
	}
	return out
}

func (w *world) allocScheduler(policy schedule.Policy) app.Scheduler {
	return app.Scheduler{
		Resources: []schedule.Resource{
			{ID: schedule.PlacementOf(placementCluster), Capacity: w.profile.slots},
			{ID: schedule.PlacementOf(placementSlots), Capacity: w.profile.slots},
		},
		Policy: policy, Demand: allocDemand{},
	}
}

// commandTimeout is the time limit of a command that sets none, which is the bound it has in an allocation.
func (w *world) commandTimeout() time.Duration {
	if w.profile.allocs {
		return jobBound
	}
	return 0
}

// submitWeight is how many times a submission is offered: once, and in a profile of allocations only
// while one runs and now and then, so that jobs come a few at a time over the whole of the run, and
// meet allocations of both placements, and not all before the second one started.
func (w *world) submitWeight() int {
	if w.alloc == nil {
		return 1
	}
	if w.rng.IntN(4) != 0 {
		return 0
	}
	for _, al := range w.alloc.state().Allocations() {
		if _, running := al.Phase.(alloc.Running); running {
			return 3
		}
	}
	return 0
}

// timeWeight is how many more times than any other move a profile offers time passing.
func (w *world) timeWeight() int {
	if w.profile.allocs {
		return 1
	}
	return 0
}

// concurrency is the most commands that may run at once: the jobs of the plain path hold slots of
// the two placements, and a job in an allocation holds nothing of its own, one to an allocation.
func (w *world) concurrency() int {
	if w.profile.allocs {
		return 2*w.profile.slots + maxHeld*len(runs)*2
	}
	return max(1, w.profile.slots)
}

// gatedAllocator is the cluster as a daemon reaches it.
type gatedAllocator struct {
	w     *world
	d     *incarnation
	inner environment.Allocator
}

func (g gatedAllocator) Request(ctx context.Context, spec alloc.Spec) (alloc.Handle, error) {
	return gated(ctx, g.w, g.d, "alloc.Request "+string(spec.ID), true, func() (alloc.Handle, error) { return g.inner.Request(ctx, spec) })
}

func (g gatedAllocator) Observe(ctx context.Context, h alloc.Handle) (alloc.Observation, error) {
	return gated(ctx, g.w, g.d, "alloc.Observe "+string(h.Alloc), false, func() (alloc.Observation, error) { return g.inner.Observe(ctx, h) })
}

func (g gatedAllocator) Release(ctx context.Context, h alloc.Handle) error {
	_, err := gated(ctx, g.w, g.d, "alloc.Release "+string(h.Alloc), true, func() (struct{}, error) { return struct{}{}, g.inner.Release(ctx, h) })
	return err
}

func (g gatedAllocator) Adopt(ctx context.Context) ([]alloc.Handle, error) {
	return gated(ctx, g.w, g.d, "alloc.Adopt", false, func() ([]alloc.Handle, error) { return g.inner.Adopt(ctx) })
}

// gatedAllocLog is the allocation log as a daemon reaches it. An append that fails leaves nothing in
// the log (store.AllocLog), so it is never a call that takes effect and fails: it is marked a read
// for the driver, which gives such a call no such fate.
type gatedAllocLog struct {
	w     *world
	d     *incarnation
	inner *memstore.AllocLog
}

func (g gatedAllocLog) Append(ctx context.Context, in alloc.Input) error {
	_, err := gated(ctx, g.w, g.d, fmt.Sprintf("alloclog.Append %T", in.Event), false, func() (struct{}, error) { return struct{}{}, g.inner.Append(ctx, in) })
	return err
}

func (g gatedAllocLog) Load(ctx context.Context) ([]alloc.Input, error) {
	return gated(ctx, g.w, g.d, "alloclog.Load", false, func() ([]alloc.Input, error) { return g.inner.Load(ctx) })
}

// noteLaunch records the allocation a job was launched in, and checks a job that asked for none is
// launched in none.
func (w *world) noteLaunch(l lifecycle.Launch) {
	w.mu.Lock()
	w.launches[l.Ref()] = l.Allocation
	w.mu.Unlock()
	if l.Allocation != "" {
		w.tracef("job %s is launched in %s", l.Ref(), l.Allocation)
	}
	if l.Spec.Allocation == "" && l.Allocation != "" {
		w.violate("job %s asked for no allocation and was launched in %s", l.Ref(), l.Allocation)
	}
}

// checkAllocationStart checks a command that starts in an allocation starts there as the allocation's
// current job, while it takes jobs, or after it ended and the job is on its way to being cancelled.
func (w *world) checkAllocationStart(ref lifecycle.Ref) {
	if w.alloc == nil {
		return
	}
	w.mu.Lock()
	handle := w.launches[ref]
	w.mu.Unlock()
	if handle == "" {
		return
	}
	var found *alloc.Allocation
	all := w.alloc.state().Allocations()
	for i := range all {
		if all[i].Handle.Ref == handle {
			found = &all[i]
		}
	}
	if found == nil {
		w.violate("job %s started in allocation %s, which no record has", ref, handle)
		return
	}
	live, ok := liveOf(*found)
	w.tracef("job %s starts in allocation %s", ref, found.ID)
	switch {
	case found.Stage() == alloc.StageEnded:
		// Its end is being acted on: the job is lost, and cancelled.
	case !ok || live.Current == nil || live.Current.Job != core.JobID(ref.String()):
		w.violate("job %s started in allocation %s, whose current job is %v", ref, found.ID, currentOf(live, ok))
	}
}

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

func currentOf(l alloc.Live, ok bool) any {
	if !ok || l.Current == nil {
		return nil
	}
	return l.Current.Job
}

// pickRun is the run that submits a job: in a profile of allocations, mostly one that has an
// allocation running, since that is where a job goes.
func (w *world) pickRun() string {
	if w.alloc != nil && w.rng.IntN(4) != 0 {
		var with []string
		for _, al := range w.alloc.state().Allocations() {
			if _, ok := al.Phase.(alloc.Running); ok && !slices.Contains(with, string(al.Run)) {
				with = append(with, string(al.Run))
			}
		}
		if len(with) > 0 {
			slices.Sort(with)
			return with[w.rng.IntN(len(with))]
		}
	}
	return runs[w.rng.IntN(len(runs))]
}

// pickAllocation says which allocation a submission asks for: none, any, or one by name, which may
// be one that is gone or never was.
func (w *world) pickAllocation(run string) string {
	if w.alloc == nil {
		return ""
	}
	switch w.rng.IntN(6) {
	case 0:
		return ""
	case 1, 2:
		return core.AllocationAny
	}
	// Mostly one that runs, which is where a job goes; sometimes any, which is where it does not.
	var ids, running []string
	for _, al := range w.alloc.state().Allocations() {
		if al.Run == core.RunID(run) {
			ids = append(ids, string(al.ID))
			if _, ok := al.Phase.(alloc.Running); ok {
				running = append(running, string(al.ID))
			}
		}
	}
	if len(running) > 0 && w.rng.IntN(4) != 0 {
		return running[w.rng.IntN(len(running))]
	}
	if len(ids) == 0 {
		return fmt.Sprintf("a%d", 1+w.rng.IntN(3))
	}
	return ids[w.rng.IntN(len(ids))]
}

// allocCall is a client call about allocations, as an actor. do returns what its end means, or a
// violation; a connection that could not be made, or that broke, means nothing.
func (w *world) allocCall(kind string, do func(ctx context.Context, c *client.Client) string) {
	w.start(kind, func(ctx context.Context) string {
		c, err := client.Connect(ctx, w.options())
		if err != nil {
			return ""
		}
		defer c.Close()
		return do(ctx, c)
	})
}

// allocFailure says what an error of an allocation call means: nothing for an error the call may
// meet, and a violation for a failure of the daemon's own that no fault of the world explains.
func allocFailure(what string, err error) string {
	var f *core.Failure
	if err == nil || !errors.As(err, &f) {
		return ""
	}
	if f.Kind == core.FailureInternal {
		return fmt.Sprintf("%s failed as the daemon's own mistake: %v", what, err)
	}
	return ""
}

// allocActions are the things runs and the world do with allocations.
func (w *world) allocActions() []option {
	a := w.alloc
	var out []option
	add := func(label string, do func()) { out = append(out, option{label, do}) }
	all := a.state().Allocations()
	run := runs[w.rng.IntN(len(runs))]
	if a.requests < 10 {
		placement := []string{placementCluster, placementSlots}[w.rng.IntN(2)]
		d := []time.Duration{10 * time.Minute, 20 * time.Minute, 40 * time.Minute, maxFor}[w.rng.IntN(4)]
		add(fmt.Sprintf("%s requests %s for %s", run, placement, d), func() { w.requestAllocation(run, placement, d) })
	}
	if len(all) > 0 {
		al := all[w.rng.IntN(len(all))]
		after := w.rng.IntN(2) == 0
		// Runs end allocations seldom, so that jobs meet allocations that run.
		if w.rng.IntN(30) == 0 {
			add(fmt.Sprintf("%s ends %s (after jobs %v)", al.Run, al.ID, after), func() { w.endAllocation(string(al.Run), al.ID, after) })
		}
		order := &cliv1.RenewalOrder{
			Lead:         durationpb.New(time.Duration(5+w.rng.IntN(20)) * time.Minute),
			SuccessorFor: durationpb.New(time.Duration(30+w.rng.IntN(60)) * time.Minute),
		}
		if w.rng.IntN(2) == 0 {
			order.Until = &cliv1.RenewalOrder_RunEnd{RunEnd: true}
		} else {
			order.Until = &cliv1.RenewalOrder_At{At: timestamppb.New(w.clock.Now().Add(time.Duration(1+w.rng.IntN(6)) * time.Hour))}
		}
		if al.Placement == placementCluster && w.rng.IntN(3) == 0 {
			add(fmt.Sprintf("%s renews %s", al.Run, al.ID), func() { w.renewAllocation(string(al.Run), al.ID, order) })
		}
		if w.rng.IntN(10) == 0 {
			add(fmt.Sprintf("%s takes the renewal of %s away", al.Run, al.ID), func() { w.renewAllocation(string(al.Run), al.ID, nil) })
		}
		if _, running := al.Phase.(alloc.Running); running && al.Handle != (alloc.Handle{}) && al.Placement == placementCluster && w.rng.IntN(8) == 0 {
			add("someone cancels "+string(al.ID), func() {
				a.mu.Lock()
				a.cancelled[al.Handle.Ref] = true
				a.mu.Unlock()
				a.fake.CancelOutside(al.Handle)
			})
		}
	}
	if len(all) >= 3 && w.rng.IntN(25) == 0 {
		add(run+" ends", func() { w.endRun(run) })
	}
	add("the cluster flips", func() {
		a.mu.Lock()
		a.down = !a.down
		down := a.down
		a.mu.Unlock()
		a.fake.SetUnreachable(down)
	})
	return out
}

func (w *world) requestAllocation(run, placement string, d time.Duration) {
	a := w.alloc
	a.requests++
	endedBefore := a.runEndedNow(run)
	w.allocCall("request", func(ctx context.Context, c *client.Client) string {
		_, err := c.RequestAllocation(ctx, &cliv1.RequestAllocationRequest{
			RunId: run, Request: &cliv1.AllocationRequest{Placement: placement, RequestedFor: durationpb.New(d)},
		})
		if err == nil && endedBefore {
			return fmt.Sprintf("%s asked for an allocation after its end was answered, and got it", run)
		}
		return allocFailure("RequestAllocation", err)
	})
}

func (a *allocWorld) runEndedNow(run string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.runEnded[run]
}

func (w *world) endAllocation(run string, id alloc.ID, after bool) {
	a := w.alloc
	a.mu.Lock()
	a.endAsked[id] = true
	a.mu.Unlock()
	w.allocCall("end", func(ctx context.Context, c *client.Client) string {
		_, err := c.EndAllocation(ctx, &cliv1.EndAllocationRequest{RunId: run, End: &cliv1.AllocationEnd{Id: string(id), AfterJobs: after}})
		return allocFailure("EndAllocation", err)
	})
}

func (w *world) renewAllocation(run string, id alloc.ID, order *cliv1.RenewalOrder) {
	w.allocCall("renew", func(ctx context.Context, c *client.Client) string {
		req := &cliv1.RenewAllocationRequest{RunId: run, Id: string(id)}
		if order == nil {
			req.Order = &cliv1.RenewAllocationRequest_Off{Off: true}
		} else {
			req.Order = &cliv1.RenewAllocationRequest_Renewal{Renewal: order}
		}
		_, err := c.RenewAllocation(ctx, req)
		return allocFailure("RenewAllocation", err)
	})
}

// endRun is a run ending, as whoever ends it asks: each of its allocations ends at once, and it asks
// for no more. Once answered, nothing it asks for is granted.
func (w *world) endRun(run string) {
	a := w.alloc
	a.mu.Lock()
	a.runEnding[run] = true
	a.mu.Unlock()
	// It is asked again until it is answered: a daemon may die under the call, as an operator's
	// forget is (forgetAttempts).
	w.start("end-run", func(ctx context.Context) string {
		for range forgetAttempts {
			if ctx.Err() != nil {
				return ""
			}
			c, err := client.Connect(ctx, w.options())
			if err != nil {
				continue
			}
			resp, err := c.EndRunAllocations(ctx, &cliv1.EndRunAllocationsRequest{RunId: run})
			c.Close()
			if err != nil {
				if v := allocFailure("EndRunAllocations", err); v != "" {
					return v
				}
				continue
			}
			a.mu.Lock()
			a.runEnded[run] = true
			a.mu.Unlock()
			for _, al := range resp.GetAllocations() {
				if al.GetState() != cliv1.AllocationState_ALLOCATION_STATE_ENDED {
					return fmt.Sprintf("the end of %s left %s %s", run, al.GetId(), al.GetState())
				}
			}
			return ""
		}
		return ""
	})
}

// checkAllocs is what must hold of the allocations after every step.
func (w *world) checkAllocs() {
	a := w.alloc
	if a == nil {
		return
	}
	state := a.state()
	all := state.Allocations()
	held := map[string]int{}
	byID := map[alloc.ID]alloc.Allocation{}
	for _, al := range all {
		byID[al.ID] = al
		if a.stages[al.ID] != al.Stage() {
			a.stages[al.ID] = al.Stage()
			w.tracef("allocation %s %s (%s %s)", al.ID, al.Stage(), al.Run, al.Placement)
		}
		if al.Stage() != alloc.StageEnded {
			held[string(al.Run)+" "+al.Placement]++
		}
		w.checkEndedFor(al)
		w.checkRenewalOf(al, byID)
	}
	for key, n := range held {
		if n > maxHeld {
			w.violate("%s holds %d allocations, and max_held is %d", key, n, maxHeld)
		}
	}
	w.checkDaemonAllocations(all)
	w.checkBackend(all)
	w.checkOneJobAtATime()
}

// checkEndedFor checks an allocation that ended ended for a reason that happened, and that it
// stays as it ended.
func (w *world) checkEndedFor(al alloc.Allocation) {
	a := w.alloc
	end, ok := al.Phase.(alloc.Ended)
	if !ok {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if was, seen := a.ended[al.ID]; seen {
		if was.Reason != end.Reason || !was.At.Equal(end.At) {
			w.violate("allocation %s ended as %s and is now ended as %s", al.ID, was.Reason, end.Reason)
		}
		return
	}
	a.ended[al.ID] = end
	w.tracef("allocation %s ended: %s", al.ID, end.Reason)
	switch end.Reason {
	case alloc.EndRequested:
		if !a.endAsked[al.ID] && !a.runEnding[string(al.Run)] {
			w.violate("allocation %s ended as requested, and nobody asked", al.ID)
		}
	case alloc.EndTimeLimit:
		// One that ended between two looks was never seen to start: it started at the earliest when it was
		// asked for.
		since := end.StartedAt
		if since.IsZero() {
			since = al.RequestedAt
		}
		if w.clock.Now().Before(since.Add(al.RequestedFor)) {
			w.violate("allocation %s ended by its time limit before it had run for %s", al.ID, al.RequestedFor)
		}
	case alloc.EndExternal:
		if !a.cancelled[al.Handle.Ref] {
			w.violate("allocation %s (%s) ended as external, and nobody cancelled it outside", al.ID, al.Handle.Ref)
		}
	case alloc.EndQueueTimeout:
		w.violate("allocation %s ended by a queue timeout, which no placement has", al.ID)
	}
}

// checkRenewalOf checks an allocation has at most one successor, which never changes, that is its
// own and another's.
func (w *world) checkRenewalOf(al alloc.Allocation, byID map[alloc.ID]alloc.Allocation) {
	a := w.alloc
	if al.RenewedBy == "" {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if was, ok := a.renewedBy[al.ID]; ok && was != al.RenewedBy {
		w.violate("allocation %s was renewed by %s and now by %s", al.ID, was, al.RenewedBy)
	}
	if _, ok := a.renewedBy[al.ID]; !ok {
		w.tracef("allocation %s renewed by %s", al.ID, al.RenewedBy)
	}
	a.renewedBy[al.ID] = al.RenewedBy
	if al.RenewedBy == al.ID {
		w.violate("allocation %s is its own successor", al.ID)
	}
	if next, ok := byID[al.RenewedBy]; ok && (next.Run != al.Run || next.Placement != al.Placement) {
		w.violate("allocation %s was renewed by %s, which is another run's or placement's", al.ID, al.RenewedBy)
	}
}

// checkDaemonAllocations checks a daemon that serves has exactly the allocations its log folds to,
// which is what a restart finds again.
func (w *world) checkDaemonAllocations(folded []alloc.Allocation) {
	w.mu.Lock()
	d := w.daemon
	w.mu.Unlock()
	if d == nil || d.dead.Load() {
		return
	}
	select {
	case <-d.serving:
	default:
		return
	}
	views, err := d.app.Allocations("", true)
	if err != nil {
		w.violate("the daemon keeps no allocations: %v", err)
		return
	}
	if len(views) != len(folded) {
		w.violate("the daemon has %d allocations, and its log folds to %d", len(views), len(folded))
		return
	}
	for i := range views {
		got, want := wire.FromAllocation(views[i].Allocation), wire.FromAllocation(folded[i])
		if !proto.Equal(got, want) {
			w.violate("the daemon has allocation %v, and its log folds to %v", got, want)
		}
	}
}

// checkBackend checks nothing the cluster holds is leaked: every allocation it holds is a record's
// that was not released.
func (w *world) checkBackend(all []alloc.Allocation) {
	for _, h := range w.alloc.fake.Live() {
		i := slices.IndexFunc(all, func(al alloc.Allocation) bool { return al.ID == h.Alloc })
		if i < 0 {
			w.violate("the cluster holds %s, which no record names", h.Alloc)
			continue
		}
		if end, ok := all[i].Phase.(alloc.Ended); ok && end.Released {
			w.violate("the cluster holds %s, whose release was confirmed", h.Alloc)
		}
	}
}

// checkOneJobAtATime checks no allocation runs two commands at once.
func (w *world) checkOneJobAtATime() {
	byToken := map[string]lifecycle.Ref{}
	for _, l := range w.jobs() {
		byToken[tokenOf(l.job)] = w.refIn(l.scope, l.job.ID)
	}
	in := map[string]int{}
	for _, x := range w.env.ActiveRuns() {
		if len(x.Argv) < 2 {
			continue
		}
		ref, ok := byToken[strings.SplitN(x.Argv[1], "-", 2)[0]]
		if !ok {
			continue
		}
		w.mu.Lock()
		handle := w.launches[ref]
		w.mu.Unlock()
		if handle != "" {
			in[handle]++
		}
	}
	for handle, n := range in {
		if n > 1 {
			w.violate("%d commands run at once in allocation %s", n, handle)
		}
	}
}

// healAllocs is the world healing: the cluster answers, and every run ends, which ends what it
// holds.
func (w *world) healAllocs() {
	w.alloc.mu.Lock()
	w.alloc.down = false
	w.alloc.mu.Unlock()
	w.alloc.fake.SetUnreachable(false)
	for _, run := range runs {
		w.endRun(run)
	}
}

// allocsSettled says every allocation ended and its release is confirmed.
func (w *world) allocsSettled() bool {
	if w.alloc == nil {
		return true
	}
	for _, al := range w.alloc.state().Allocations() {
		if end, ok := al.Phase.(alloc.Ended); !ok || !end.Released {
			return false
		}
	}
	return len(w.alloc.fake.Live()) == 0
}

// allocsUnsettled says what is left of the allocations: those not ended and released, and what the
// cluster holds.
func (w *world) allocsUnsettled() []string {
	if w.alloc == nil {
		return nil
	}
	var out []string
	for _, al := range w.alloc.state().Allocations() {
		if end, ok := al.Phase.(alloc.Ended); !ok || !end.Released {
			out = append(out, fmt.Sprintf("allocation %s %s %+v", al.ID, al.Stage(), al.Phase))
		}
	}
	for _, h := range w.alloc.fake.Live() {
		out = append(out, fmt.Sprintf("the cluster holds %s", h.Alloc))
	}
	return out
}

// finalAllocCheck is what must hold when the world healed and every run ended: nothing of any run is
// left, and no job is held by an allocation.
func (w *world) finalAllocCheck() {
	if w.alloc == nil {
		return
	}
	for _, al := range w.alloc.state().Allocations() {
		if _, ok := al.Phase.(alloc.Ended); !ok {
			w.violate("allocation %s of run %s is %s after the run ended", al.ID, al.Run, al.Stage())
		}
	}
	if live := w.alloc.fake.Live(); len(live) != 0 {
		w.violate("the cluster holds %v after every run ended", live)
	}
	for _, run := range runs {
		if !w.alloc.runEndedNow(run) {
			w.violate("the end of %s was never answered", run)
		}
	}
}

// Seeds of allocations hold the invariants. The default range must exercise what the checks are
// about, or they hold of nothing: jobs that ran in allocations, allocations that ended by their time
// limit, by someone's cancel and by the run's end, a successor made by a renewal, and a restart.
// allocDefaultSeeds is the default range of the allocation seeds: each runs a long script, and under
// the race detector the whole package must still fit its timeout.
const allocDefaultSeeds = 30

func TestSeedsOfAllocationsHoldTheInvariants(t *testing.T) {
	t.Parallel()
	first, count := seedRange()
	count = min(count, allocDefaultSeeds)
	got := map[string]int{}
	verbose := os.Getenv("VIBESYS_SIM_TRACE") != ""
	for seed := first; seed < first+count; seed++ {
		var violation, trace string
		synctest.Test(t, func(*testing.T) { violation, trace = simulateWith(seed, allocationsOf) })
		for _, m := range allocationMarks {
			if strings.Contains(trace, m.text) {
				got[m.name]++
			}
		}
		if verbose && (violation != "" || os.Getenv("VIBESYS_SIM_TRACE") == "all") {
			t.Log(trace)
		}
		if violation != "" {
			t.Errorf("seed %d: %s", seed, violation)
		}
	}
	for _, m := range allocationMarks {
		t.Logf("%d of %d seeds: %s", got[m.name], count, m.name)
		if count == allocDefaultSeeds && got[m.name] == 0 {
			t.Errorf("no seed of %d: %s", count, m.name)
		}
	}
}

// allocationMarks are what a seed's trace says it did, and what the default range must reach.
var allocationMarks = []struct{ name, text string }{
	{"a run asked for an allocation of the cluster", "requests p"},
	{"a run asked for an allocation of the machine's slots", "requests q"},
	{"a job was launched in an allocation", "is launched in fake"},
	{"a job started in an allocation", "starts in allocation"},
	{"a job in an allocation of the machine's slots was launched", "is launched in slot"},
	{"an allocation ended by its time limit", ": time_limit"},
	{"an allocation ended as someone cancelled it", ": external"},
	{"an allocation ended as its run asked", ": requested"},
	{"an allocation was renewed", "renewed by"},
	{"the daemon restarted", "daemon 2 starts"},
}
