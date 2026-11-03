package production

import (
	"context"
	"sort"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// Subscription is one CLI task stream, shared by every live run at this location.
type Subscription struct {
	Location         core.CliLocation
	Repository, Task string
}

func subscription(r core.Run) Subscription { return Subscription{r.Location, r.Repository, r.Task} }

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
			x, y := []string{a.Location.CLI, a.Location.RuntimeDir, a.Location.StateHome, a.Location.Home, a.Repository, a.Task}, []string{b.Location.CLI, b.Location.RuntimeDir, b.Location.StateHome, b.Location.Home, b.Repository, b.Task}
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
type Reconciler struct {
	Store  *search.Store
	Follow func(context.Context, Subscription, func(search.View))
	mu     sync.Mutex
	active map[Subscription]*following
	runs   map[core.RunID]Subscription
}
type following struct {
	cancel context.CancelFunc
	done   chan struct{}
	view   search.View
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
	for id := range r.runs {
		if _, ok := next[id]; !ok {
			r.Store.Delete(id)
		}
	}
	r.runs = next
	for id, k := range next {
		if a := r.active[k]; a != nil {
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
		a := &following{cancel: cancel, done: make(chan struct{})}
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
	r.mu.Unlock()
	for _, a := range stopped {
		<-a.done
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
	r.mu.Unlock()
	for _, a := range all {
		<-a.done
	}
}

// SearchFor wires the production edges around the read-only link library.
func SearchFor(executable string) *Reconciler {
	return &Reconciler{Store: new(search.Store), Follow: func(ctx context.Context, k Subscription, publish func(search.View)) {
		loc := clidaemon.Location{XDGRuntimeDir: k.Location.RuntimeDir, XDGStateHome: k.Location.StateHome, Home: k.Location.Home}
		var view search.View
		f := clidaemon.Follower{
			Location: loc, Ensurer: ExecEnsurer{Executable: executable, Runner: ExecRunner{}},
			Connect: func(ctx context.Context) (clidaemon.Connection, error) { return connectCLI(ctx, loc) },
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
		case clidaemon.NoDaemon:
			view.Reason = search.NoDaemon
		case clidaemon.StartFailed:
			view.Reason = search.StartFailed
		case clidaemon.CLINotInstalled:
			view.Reason = search.CLINotInstalled
		case clidaemon.ClientTooOld:
			view.Reason = search.ClientTooOld
		case clidaemon.DaemonTooOld:
			view.Reason = search.DaemonTooOld
		case clidaemon.StreamLost:
			view.Reason = search.StreamLost
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
