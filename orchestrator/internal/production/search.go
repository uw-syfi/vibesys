package production

import (
	"cmp"
	"context"
	"errors"
	"slices"
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

// subscription is the task stream a run follows: its task, or while it is in setup, the name its lead
// has declared for the task it is making, which is empty until it declares one. A run with an empty
// name follows nothing (see hasTask).
func subscription(r core.Run) Subscription {
	task := r.Task
	if task == "" {
		task = r.SetupTask
	}
	return Subscription{r.JobTool, r.Repository, task}
}

// hasTask says the run has a task to follow.
func hasTask(r core.Run) bool { return subscription(r).Task != "" }

// over says the run has ended or failed: nothing it did can still reach the job log. A run that is
// stopping is not over, since its agents may yet record a result as they stop.
func over(r core.Run) bool { return r.Status.Final() }

// Plan computes deterministic start/stop sets, independent of run multiplicity. A task is followed
// for as long as one of its runs is not over.
func Plan(runs []core.Run, followed []Subscription) (start, stop []Subscription) {
	wanted := map[Subscription]bool{}
	current := map[Subscription]bool{}
	for _, r := range runs {
		if !over(r) && hasTask(r) {
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
// A run's view is the job log of its task, as the job service held it. While a run is not over the
// view follows the task's stream; once it is over (see over) the view is read once, by a read that
// opened after the run was seen over, and that read is the run's last: a run that ended while this host
// was running and one that ended before it started are read the same way, so an ended run's facts do
// not depend on which host saw it end, and a view that is still being read always ends. A read covers
// the runs that were over when it opened and no others: it cannot hold what a run did after it opened,
// so a run of a task that an earlier run already ended on is read again, never given the earlier read.
// ReadBound, when set, bounds each read, so a job service that accepts the connection and never sends
// settles as unreadable.
type Reconciler struct {
	Store     *search.Store
	Follow    func(context.Context, Subscription, func(search.View))
	ReadBound func(context.Context) (context.Context, context.CancelFunc)
	mu        sync.Mutex
	active    map[Subscription]*following
	runs      map[core.RunID]Subscription
	reads     []*read
	// ended is the runs that are over and have been dealt with by this host: given their read, or an
	// empty view when they never opened.
	ended map[core.RunID]bool
}
type following struct {
	cancel context.CancelFunc
	done   chan struct{}
	view   search.View
}

// read is one look at a task's job log, made for the runs that were over when it opened.
type read struct {
	cancel context.CancelFunc
	done   chan struct{}
	// covers is the runs whose view the read settles.
	covers []core.RunID
	// kept is what the host held of each covered run when the read opened: the facts the run keeps when
	// the read cannot be made. It is dropped once the read settles.
	kept    map[core.RunID]search.Facts
	settled bool
}

func (r *Reconciler) Sync(ctx context.Context, runs []core.Run) {
	r.mu.Lock()
	keys := make([]Subscription, 0, len(r.active))
	for k := range r.active {
		keys = append(keys, k)
	}
	starts, stops := Plan(runs, keys)
	next := map[core.RunID]Subscription{}
	for _, run := range runs {
		if !over(run) && hasTask(run) {
			next[run.ID] = subscription(run)
		}
	}
	known := map[core.RunID]bool{}
	for _, run := range runs {
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
	r.readOver(ctx, runs)
	r.mu.Unlock()
	for _, a := range stopped {
		<-a.done
	}
}

// readOver deals with the runs that have just been seen over: a run that never opened has an empty
// view, which is a read, and the others are read together, once for each task. It is called with mu held.
func (r *Reconciler) readOver(ctx context.Context, runs []core.Run) {
	byTask := map[Subscription][]core.Run{}
	var tasks []Subscription
	for _, run := range runs {
		if !over(run) || r.ended[run.ID] {
			continue
		}
		if r.ended == nil {
			r.ended = map[core.RunID]bool{}
		}
		r.ended[run.ID] = true
		if run.NeverOpened || !hasTask(run) {
			// Its lead never ran a turn, so nothing could have submitted a job, or it named no task, so
			// no job of a task could be its own: that is a read.
			r.Store.Put(run.ID, search.View{})
			continue
		}
		k := subscription(run)
		if byTask[k] == nil {
			tasks = append(tasks, k)
		}
		byTask[k] = append(byTask[k], run)
	}
	slices.SortFunc(tasks, func(a, b Subscription) int {
		return cmp.Or(
			cmp.Compare(a.JobTool.Executable, b.JobTool.Executable), cmp.Compare(a.JobTool.RuntimeDirectory, b.JobTool.RuntimeDirectory),
			cmp.Compare(a.Repository, b.Repository), cmp.Compare(a.Task, b.Task))
	})
	for _, k := range tasks {
		r.startRead(ctx, k, byTask[k])
	}
}

// startRead opens the read of a task's job log that settles the given runs, which are over. It is called
// with mu held.
func (r *Reconciler) startRead(ctx context.Context, k Subscription, runs []core.Run) {
	rd := &read{done: make(chan struct{}), kept: map[core.RunID]search.Facts{}}
	for _, run := range runs {
		// What the run's own view knew stays when the last read cannot be made.
		prior, rev := r.Store.Get(run.ID)
		if a := r.active[k]; a != nil && rev == 0 {
			prior = a.view
		}
		if rev == 0 {
			r.Store.Put(run.ID, search.View{Reason: core.Connecting})
		}
		rd.covers = append(rd.covers, run.ID)
		rd.kept[run.ID] = prior.Facts
	}
	c, cancel := context.WithCancel(ctx)
	bound := func() {}
	if r.ReadBound != nil {
		c, bound = r.ReadBound(c)
	}
	rd.cancel = cancel
	// A read whose goroutine has ended is no longer held.
	r.reads = slices.DeleteFunc(r.reads, func(old *read) bool {
		select {
		case <-old.done:
			return true
		default:
			return false
		}
	})
	r.reads = append(r.reads, rd)
	settle := func(v search.View) {
		for _, id := range rd.covers {
			if !r.ended[id] {
				continue // the host has let the run go.
			}
			put := v
			if v.Reason != core.Readable && len(v.Facts) == 0 {
				put.Facts = rd.kept[id]
			}
			r.Store.Put(id, put)
		}
		rd.settled, rd.kept = true, nil
		cancel()
	}
	go func() {
		defer close(rd.done)
		defer bound()
		r.Follow(c, k, func(v search.View) {
			r.mu.Lock()
			defer r.mu.Unlock()
			if !rd.settled {
				settle(v)
			}
		})
		r.mu.Lock()
		defer r.mu.Unlock()
		// A read that ran out of time ends as a failure to read. One cancelled by Close ends as nothing.
		if !rd.settled && errors.Is(c.Err(), context.DeadlineExceeded) {
			settle(search.View{Reason: core.StreamLost, Detail: "the job service did not send the job log in time"})
		}
	}()
}

// ReadJobLog reads the whole job log of a run's task now, on a stream of its own: the first view a stream
// gives is the whole state at the moment it opened, so every result recorded before the call is in it,
// which a view kept from a stream that may be behind does not promise. The stream closes when it has
// answered. It returns the context's error when the context ends first.
func (r *Reconciler) ReadJobLog(ctx context.Context, run core.Run) (search.View, error) {
	if !hasTask(run) {
		// A run in setup whose lead has named no task has no job log: that is a read, of nothing.
		return search.View{}, nil
	}
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
	reads := r.reads
	r.reads = nil
	for _, rd := range reads {
		rd.cancel()
	}
	r.mu.Unlock()
	for _, a := range all {
		<-a.done
	}
	for _, rd := range reads {
		<-rd.done
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
