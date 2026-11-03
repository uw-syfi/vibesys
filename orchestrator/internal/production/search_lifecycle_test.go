package production

import (
	"context"
	"math/rand/v2"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// test-isolation: exercise the private follower adapter's failure mapping and late
// callback guard; synctest owns scheduling, with no wall-clock synchronization.
func TestFollowerEveryFailureReplacesReadableReason(t *testing.T) {
	for _, reason := range []clidaemon.Reason{clidaemon.NoDaemon, clidaemon.StartFailed, clidaemon.CLINotInstalled, clidaemon.ClientTooOld, clidaemon.DaemonTooOld, clidaemon.StreamLost, clidaemon.MessageTooLarge, 255} {
		ctx, cancel := context.WithCancel(context.Background())
		v := search.View{Reason: search.Readable}
		f := clidaemon.Follower{
			Connect: func(context.Context) (clidaemon.Connection, error) {
				return nil, &clidaemon.Unreadable{Reason: reason, Detail: "failure"}
			},
			Bound: func(c context.Context) (context.Context, context.CancelFunc) {
				return context.WithDeadline(c, time.Unix(1<<40, 0))
			},
			Ensurer: missingEnsurer{reason},
			Sleep:   func(context.Context, time.Duration) error { cancel(); return context.Canceled },
		}
		followSearch(ctx, f, Subscription{Repository: "/repo", Task: "task"}, &v, func(search.View) {})
		cancel()
		if v.Reason == search.Readable || v.Detail != "failure" {
			t.Fatalf("reason %v left %+v", reason, v)
		}
	}
}

type missingEnsurer struct{ reason clidaemon.Reason }

func (e missingEnsurer) Ensure(context.Context, clidaemon.Location) error {
	return &clidaemon.Unreadable{Reason: e.reason, Detail: "failure"}
}

func TestReconcilerGeneratedLifetimes(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		random := rand.New(rand.NewPCG(19, 42))
		store := new(search.Store)
		var mu sync.Mutex
		active := map[Subscription]bool{}
		callbacks := map[Subscription]func(search.View){}
		r := &Reconciler{Store: store}
		r.Follow = func(ctx context.Context, k Subscription, publish func(search.View)) {
			mu.Lock()
			if active[k] {
				t.Error("duplicate follower")
			}
			active[k] = true
			callbacks[k] = publish
			mu.Unlock()
			defer func() { mu.Lock(); delete(active, k); mu.Unlock() }()
			fake := clidaemontest.New()
			f := clidaemon.Follower{Connect: func(context.Context) (clidaemon.Connection, error) { return fake, nil }, Bound: func(c context.Context) (context.Context, context.CancelFunc) {
				return context.WithDeadline(c, time.Unix(1<<40, 0))
			}, Sleep: func(c context.Context, _ time.Duration) error { <-c.Done(); return c.Err() }}
			v := search.View{}
			followSearch(ctx, f, k, &v, publish)
			// A callback racing a stop must not restore a deleted run.
			publish(search.View{Reason: search.StreamLost, Detail: "late"})
		}
		for range 100 {
			var runs []core.Run
			want := map[Subscription]bool{}
			for i := range 5 {
				run := core.Run{ID: core.RunID(string(rune('a' + i))), Repository: "/repo", Task: "task", Location: core.CliLocation{CLI: []string{"/a/cli", "/b/cli"}[random.IntN(2)]}, Status: core.RunEnded}
				if random.IntN(2) == 0 {
					run.Status = core.RunRunning
					want[subscription(run)] = true
				}
				runs = append(runs, run)
			}
			r.Sync(context.Background(), runs)
			synctest.Wait()
			if len(active) != len(want) {
				t.Fatalf("active %v want %v", active, want)
			}
			for k := range want {
				if !active[k] {
					t.Fatal("missing follower", k)
				}
			}
			for _, run := range runs {
				v, rev := store.Get(run.ID)
				if !run.Status.Live() && rev != 0 {
					t.Fatal("ended view retained", run.ID, v)
				}
				if run.Status.Live() && rev == 0 {
					t.Fatal("missing view", run.ID)
				}
			}
		}
		r.Sync(context.Background(), nil)
		synctest.Wait()
		for _, publish := range callbacks {
			publish(search.View{Reason: search.Readable})
		}
		for _, id := range []core.RunID{"a", "b", "c", "d", "e"} {
			if _, rev := store.Get(id); rev != 0 {
				t.Fatal("late callback restored view")
			}
		}
		r.Close()
		r.Close()
		if len(active) != 0 {
			t.Fatal("leaked follower")
		}
	})
}

func TestTerminalFollowerRestartsOnlyWithChangedSubscription(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		calls := 0
		r := &Reconciler{Store: new(search.Store), Follow: func(_ context.Context, _ Subscription, publish func(search.View)) {
			calls++
			publish(search.View{Reason: search.CLINotInstalled})
		}}
		run := core.Run{ID: "r", Status: core.RunRunning, Location: core.CliLocation{CLI: "/old/cli"}}
		r.Sync(context.Background(), []core.Run{run})
		synctest.Wait()
		for range 10 {
			r.Sync(context.Background(), []core.Run{run})
			synctest.Wait()
		}
		v, _ := r.Store.Get("r")
		if calls != 1 || v.Reason != search.CLINotInstalled {
			t.Fatal(calls, v)
		}
		run.Location.CLI = "/new/cli"
		r.Sync(context.Background(), []core.Run{run})
		synctest.Wait()
		if calls != 2 {
			t.Fatal(calls)
		}
		r.Close()
	})
}

func TestNewFollowerStartsConnecting(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := &Reconciler{Store: new(search.Store), Follow: func(ctx context.Context, _ Subscription, _ func(search.View)) { <-ctx.Done() }}
		r.Sync(context.Background(), []core.Run{{ID: "r", Status: core.RunRunning}})
		v, rev := r.Store.Get("r")
		if rev == 0 || v.Reason != search.Connecting {
			t.Fatal(v, rev)
		}
		r.Close()
	})
}
