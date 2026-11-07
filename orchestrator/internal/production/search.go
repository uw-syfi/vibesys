package production

import (
	"context"
	"errors"
	"sort"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// Subscription is one CLI task stream, shared by every live run of this job tool.
type Subscription struct {
	JobTool          core.JobToolEndpoint
	Repository, Task string
}

func subscription(r core.Run) Subscription { return Subscription{r.JobTool, r.Repository, r.Task} }

// Plan computes deterministic start/stop sets, independent of run multiplicity.
func Plan(live []core.Run, followed []Subscription) (start, stop []Subscription) {
	wanted := map[Subscription]bool{}
	current := map[Subscription]bool{}
	for _, r := range live {
		if r.Status.Live() {
			wanted[subscription(r)] = true
		}
	}
	for _, k := range followed {
		current[k] = true
	}
	for k := range wanted {
		if !current[k] {
			start = append(start, k)
		}
	}
	for k := range current {
		if !wanted[k] {
			stop = append(stop, k)
		}
	}
	less := func(xs []Subscription) {
		sort.Slice(xs, func(i, j int) bool {
			a, b := xs[i], xs[j]
			x, y := []string{a.JobTool.Executable, a.JobTool.RuntimeDirectory, a.Repository, a.Task}, []string{b.JobTool.Executable, b.JobTool.RuntimeDirectory, b.Repository, b.Task}
			for n := range x {
				if x[n] != y[n] {
					return x[n] < y[n]
				}
			}
			return false
		})
	}
	less(start)
	less(stop)
	return
}

// Reconciler owns follower lifetimes. Sync is called by the daemon's single writer.
// Updates and membership changes serialize under mu so late callbacks cannot
// restore a removed run. Close cancels and joins every follower.
// Sync scans run membership each step, but only writes views when membership or
// facts change. It never compares all facts on every daemon wake.
// Terminal exits remain unreadable until the subscription is removed or its
// recorded location changes. Retrying unchanged inputs cannot repair a terminal
// protocol/install/size failure and would spin on every daemon step.
//
// A run that is over is read once by each host process, whether it ended while
// this host was running or before it started: the first view Follow publishes,
// readable or not, is its last, and it stays in Store. So an ended run's counts
// do not depend on which host saw it end, and a view that is still being read
// always ends. ReadBound, when set, bounds that one read, so a job service that
// accepts the connection and never sends settles as unreadable.
type Reconciler struct {
	Store     *search.Store
	Follow    func(context.Context, Subscription, func(search.View))
	ReadBound func(context.Context) (context.Context, context.CancelFunc)
	mu        sync.Mutex
	active    map[Subscription]*following
	runs      map[core.RunID]Subscription
	reads     map[Subscription]*following
	ended     map[core.RunID]endedRun
}

// endedRun is a run that is over and has been dealt with by this host. Read says its view is the
// shared read of its subscription, and not the empty one of a start that never ran.
type endedRun struct {
	key  Subscription
	read bool
}
type following struct {
	cancel  context.CancelFunc
	done    chan struct{}
	view    search.View
	settled bool
}

func (r *Reconciler) Sync(ctx context.Context, live []core.Run) {
	r.mu.Lock()
	keys := make([]Subscription, 0, len(r.active))
	for k := range r.active {
		keys = append(keys, k)
	}
	starts, stops := Plan(live, keys)
	next := map[core.RunID]Subscription{}
	for _, run := range live {
		if run.Status.Live() {
			next[run.ID] = subscription(run)
		}
	}
	known := map[core.RunID]bool{}
	for _, run := range live {
		known[run.ID] = true
	}
	for id := range r.ended {
		if !known[id] {
			delete(r.ended, id)
			r.Store.Delete(id)
		}
	}
	for id := range r.runs {
		if !known[id] {
			r.Store.Delete(id)
		}
	}
	oldRuns := r.runs
	r.runs = next
	for id, k := range next {
		if a := r.active[k]; a != nil {
			if previous, existed := oldRuns[id]; existed && previous == k {
				continue
			}
			r.Store.Put(id, a.view)
		}
	}
	var stopped []*following
	for _, k := range stops {
		a := r.active[k]
		delete(r.active, k)
		a.cancel()
		stopped = append(stopped, a)
	}
	if r.active == nil {
		r.active = map[Subscription]*following{}
	}
	for _, k := range starts {
		c, cancel := context.WithCancel(ctx)
		a := &following{cancel: cancel, done: make(chan struct{}), view: search.View{Reason: core.Connecting}}
		for id, key := range next {
			if key == k {
				r.Store.Put(id, a.view)
			}
		}
		r.active[k] = a
		go func() {
			defer close(a.done)
			r.Follow(c, k, func(v search.View) {
				r.mu.Lock()
				defer r.mu.Unlock()
				if r.active[k] != a {
					return
				}
				a.view = v
				for id, key := range r.runs {
					if key == k {
						r.Store.Put(id, v)
					}
				}
			})
		}()
	}
	for _, run := range live {
		if !run.Status.Live() {
			r.readEnded(ctx, run)
		}
	}
	r.mu.Unlock()
	for _, a := range stopped {
		<-a.done
	}
}

// readEnded makes sure a run that is over has a view in Store and, when this host has not read its
// job log yet, starts the one read. It is called with mu held.
func (r *Reconciler) readEnded(ctx context.Context, run core.Run) {
	if _, asked := r.ended[run.ID]; asked {
		return
	}
	k := subscription(run)
	if r.ended == nil {
		r.ended, r.reads = map[core.RunID]endedRun{}, map[Subscription]*following{}
	}
	r.ended[run.ID] = endedRun{k, !run.NeverOpened}
	if run.NeverOpened {
		// Its lead never ran a turn, so nothing could have submitted a job: that is a read.
		r.Store.Put(run.ID, search.View{})
		return
	}
	if a := r.reads[k]; a != nil {
		if a.settled {
			r.Store.Put(run.ID, a.view)
		} else if _, rev := r.Store.Get(run.ID); rev == 0 {
			r.Store.Put(run.ID, search.View{Reason: core.Connecting})
		}
		return
	}
	// What the run's own live view knew stays when the last read cannot be made.
	prior, rev := r.Store.Get(run.ID)
	if a := r.active[k]; a != nil && rev == 0 {
		prior = a.view
	}
	if rev == 0 {
		r.Store.Put(run.ID, search.View{Reason: core.Connecting})
	}
	c, cancel := context.WithCancel(ctx)
	bound := func() {}
	if r.ReadBound != nil {
		c, bound = r.ReadBound(c)
	}
	a := &following{cancel: cancel, done: make(chan struct{}), view: search.View{Reason: core.Connecting}}
	r.reads[k] = a
	settle := func(v search.View) {
		if v.Reason != core.Readable && len(v.Facts) == 0 {
			v.Facts = prior.Facts
		}
		a.view, a.settled = v, true
		for id, e := range r.ended {
			if e.read && e.key == k {
				r.Store.Put(id, v)
			}
		}
		cancel()
	}
	go func() {
		defer close(a.done)
		defer bound()
		r.Follow(c, k, func(v search.View) {
			r.mu.Lock()
			defer r.mu.Unlock()
			if !a.settled && r.reads[k] == a {
				settle(v)
			}
		})
		r.mu.Lock()
		defer r.mu.Unlock()
		// A read that ran out of time ends as a failure to read. One cancelled by Close ends as nothing.
		if !a.settled && r.reads[k] == a && errors.Is(c.Err(), context.DeadlineExceeded) {
			settle(search.View{Reason: core.StreamLost, Detail: "the job service did not send the job log in time"})
		}
	}()
}

// ReadJobLog reads the whole job log of a run's task now, on a stream of its own: the first view a stream
// gives is the whole state at the moment it opened, so every result recorded before the call is in it,
// which a view kept from a stream that may be behind does not promise. The stream closes when it has
// answered. It returns the context's error when the context ends first.
func (r *Reconciler) ReadJobLog(ctx context.Context, run core.Run) (search.View, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	first := make(chan search.View, 1)
	done := make(chan struct{})
	go func() {
		defer close(done)
		r.Follow(ctx, subscription(run), func(v search.View) {
			select {
			case first <- v:
			default:
			}
			cancel()
		})
	}()
	defer func() { <-done }()
	select {
	case v := <-first:
		return v, nil
	case <-ctx.Done():
		// The stream's own cancel after it answered ends the context too: the answer wins.
		select {
		case v := <-first:
			return v, nil
		default:
			return search.View{}, ctx.Err()
		}
	}
}

func (r *Reconciler) Close() {
	r.mu.Lock()
	var all []*following
	for k, a := range r.active {
		a.cancel()
		all = append(all, a)
		delete(r.active, k)
	}
	for k, a := range r.reads {
		a.cancel()
		all = append(all, a)
		delete(r.reads, k)
	}
	r.mu.Unlock()
	for _, a := range all {
		<-a.done
	}
}

// SearchFor wires the production edges around the read-only link library.
func SearchFor() *Reconciler {
	return &Reconciler{Store: new(search.Store), ReadBound: func(ctx context.Context) (context.Context, context.CancelFunc) {
		return context.WithTimeout(ctx, 30*time.Second)
	}, Follow: func(ctx context.Context, k Subscription, publish func(search.View)) {
		var view search.View
		f := clidaemon.Follower{
			Ensurer: ExecEnsurerFor(k.JobTool, ExecRunner{}),
			Connect: connectCLI,
			Bound: func(ctx context.Context) (context.Context, context.CancelFunc) {
				return context.WithTimeout(ctx, 10*time.Second)
			},
			Sleep: func(ctx context.Context, d time.Duration) error {
				timer := time.NewTimer(d)
				defer timer.Stop()
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-timer.C:
					return nil
				}
			},
		}
		followSearch(ctx, f, k, &view, publish)
	}}
}

func followSearch(ctx context.Context, f clidaemon.Follower, k Subscription, view *search.View, publish func(search.View)) {
	f.Changed = func(facts search.Facts) { *view = search.Fold(facts); publish(*view) }
	f.Failed = func(err *clidaemon.Unreadable) {
		switch err.Reason {
		case core.NoDaemon:
			view.Reason = core.NoDaemon
		case core.StartFailed:
			view.Reason = core.StartFailed
		case core.CLINotInstalled:
			view.Reason = core.CLINotInstalled
		case core.ClientTooOld:
			view.Reason = core.ClientTooOld
		case core.DaemonTooOld:
			view.Reason = core.DaemonTooOld
		case core.MessageTooLarge:
			view.Reason = core.MessageTooLarge
		default:
			view.Reason = core.StreamLost
		}
		view.Detail = err.Detail
		publish(*view)
	}
	_ = f.Follow(ctx, search.Request(k.Repository, k.Task))
}

func reconcile(r *Reconciler) func(context.Context, []core.Run) {
	if r == nil {
		return nil
	}
	return r.Sync
}

// jobLog is the reconciler as the daemon's way to read a job log afresh, or none when there is no reconciler.
func jobLog(r *Reconciler) daemon.JobLog {
	if r == nil {
		return nil
	}
	return r
}

func searchStore(r *Reconciler) *search.Store {
	if r == nil {
		return nil
	}
	return r.Store
}
