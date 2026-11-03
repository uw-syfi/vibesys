package app_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"testing"
	"testing/synctest"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/changed"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// watcher is a reader of WatchTask: it applies each send as a client does and holds the facts.
type watcher struct {
	mu     sync.Mutex
	held   map[core.FactKey]any
	sends  []core.Send
	signal changed.Signal
	stop   context.CancelFunc
	done   chan error
}

func watch(t *testing.T, a *app.App, s core.Scope) *watcher {
	t.Helper()
	ctx, cancel := context.WithCancel(ctxFor(t))
	w := &watcher{held: map[core.FactKey]any{}, stop: cancel, done: make(chan error, 1)}
	go func() {
		w.done <- a.WatchTask(ctx, s, core.DefaultJudgedCommits, func(send core.Send) error {
			w.mu.Lock()
			if send.Reset {
				w.held = map[core.FactKey]any{}
			}
			for _, f := range send.Facts {
				w.held[f.Key] = f.Value
			}
			w.sends = append(w.sends, send)
			w.mu.Unlock()
			w.signal.Notify()
			return nil
		})
	}()
	t.Cleanup(func() { cancel(); <-w.done })
	return w
}

// holds says whether what the watcher holds is the view: every fact of it, and nothing else but a
// judgment that left the judged window, which the stream does not take back.
func (w *watcher) holds(v core.TaskView) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	want := map[core.FactKey]any{}
	for _, f := range v.Facts() {
		want[f.Key] = f.Value
		if !reflect.DeepEqual(w.held[f.Key], f.Value) {
			return false
		}
	}
	if v.Content == nil {
		return true
	}
	for k := range w.held {
		if _, ok := want[k]; !ok && !strings.HasPrefix(string(k), "judgment/") {
			return false
		}
	}
	return true
}

func (w *watcher) count() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.sends)
}

// converge waits until the watcher holds the task as a look finds it. With pass set the slow
// period passes while it waits, as for a change no wake-up announces; without it the watcher has to
// have been woken.
func converge(t *testing.T, a *app.App, w *watcher, o *outage, s core.Scope, pass bool) core.TaskView {
	t.Helper()
	ctx := ctxFor(t)
	for {
		wake := w.signal.Wait()
		if v, ok := a.TaskView(ctx, s, core.DefaultJudgedCommits); ok && w.holds(v) {
			return v
		}
		if pass {
			// The watcher may not have started waiting yet, so the slow period passes again
			// until what it holds is right.
			o.limits.pass()
			runtime.Gosched()
			if ctx.Err() == nil {
				continue
			}
		}
		select {
		case <-wake:
		case <-ctx.Done():
			v, _ := a.TaskView(context.Background(), s, core.DefaultJudgedCommits)
			t.Fatalf("the watcher never held the task\nheld %+v\nview %+v", w.held, v.Facts())
		}
	}
}

// Whatever a task does, the watcher ends up holding the task, and every job that ended is among
// what it holds, ended. The operations are commits through the daemon, a repository going away and
// coming back, and a repository forgotten; the order is the seed's.
func TestAWatcherHoldsTheTaskAfterAnyInterleaving(t *testing.T) {
	for seed := uint64(1); seed <= 25; seed++ {
		t.Run(fmt.Sprintf("seed %d", seed), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				r := rand.New(rand.NewPCG(seed, seed*7919))
				w, o := newOutage(t)
				a := o.start(w.supervisors())
				wt := watch(t, a, scopeA)
				var submitted []core.JobID
				away := false
				for step := 0; step < 14; step++ {
					pass := false
					switch op := r.IntN(10); {
					case op < 5 && !away:
						job := submitAs(t, a, callA, "print", fmt.Sprint(seed, step), ";", "exit", "0")
						submitted = append(submitted, job.ID)
						// A sweep can still report the preceding outage after Submit.
						// Recovery needs the slow period even though the repository is back.
						o.awaitFinalIn(t, a, scopeA, job.ID)
					case op < 7 && !away:
						o.takeAway(repoA)
						away, pass = true, true
					case op < 8 && away:
						o.bringBack(repoA)
						away, pass = false, true
					case op < 9 && !away && len(submitted) > 0:
						o.remove(repoA)
						if _, err := a.ForgetRepository(ctxFor(t), repoA); err != nil {
							t.Fatalf("ForgetRepository: %v", err)
						}
						o.bringBackRemoved(repoA)
						submitted = nil
					default:
						pass = true
					}
					v := converge(t, a, wt, o, scopeA, pass)
					if v.Content == nil {
						continue
					}
					// Every job that is final in the store is held final.
					wt.mu.Lock()
					for _, j := range v.Content.Jobs {
						got, ok := wt.held[core.FactKey("job/"+string(j.ID))].(core.Job)
						if j.State.Final() && (!ok || got.State != j.State) {
							wt.mu.Unlock()
							t.Fatalf("step %d: the final job %s is held as %+v", step, j.ID, got)
						}
					}
					wt.mu.Unlock()
				}
			})
		})
	}
}

// A commit is announced by the commit itself: with the slow period never passing, a job that is
// submitted and one that ends are both held, so no change waits for a timer.
func TestACommitWakesAWatcherWithoutTheSlowPeriod(t *testing.T) {
	w, o := newOutage(t)
	a := o.start(w.supervisors())
	wt := watch(t, a, scopeA)
	converge(t, a, wt, o, scopeA, false)
	if wt.count() != 1 || !wt.sends[0].Reset {
		t.Fatalf("a task with nothing in it sends %+v, want one reset", wt.sends)
	}
	job := submitAs(t, a, callA, "hold", "g", ";", "exit", "0")
	converge(t, a, wt, o, scopeA, false)
	w.gates.Release("g")
	o.awaitFinal(t, a, scopeA, job.ID)
	v := converge(t, a, wt, o, scopeA, false)
	for _, j := range v.Content.Jobs {
		if got, _ := wt.held[core.FactKey("job/"+string(j.ID))].(core.Job); got.State != core.JobSucceeded {
			t.Fatalf("job %s held as %s after it succeeded", j.ID, got.State)
		}
	}
}

// failsAfterLanding is a store whose first Submit lands and then says it failed, as a store does
// whose reply is lost, or that reloads from disk after a commit that failed.
type failsAfterLanding struct {
	app.Stores
	once *sync.Once
	// before holds the first Submit until the test lets it go, so the watcher is quiet when it lands.
	before *gate
}

func (f failsAfterLanding) Create(ctx context.Context, s core.Scope) (store.Store, error) {
	st, err := f.Stores.Create(ctx, s)
	return landing{Store: st, once: f.once, before: f.before}, err
}

func (f failsAfterLanding) Find(ctx context.Context, s core.Scope) (store.Store, error) {
	st, err := f.Stores.Find(ctx, s)
	if err != nil {
		return nil, err
	}
	return landing{Store: st, once: f.once, before: f.before}, nil
}

type landing struct {
	store.Store
	once   *sync.Once
	before *gate
}

func (l landing) Submit(ctx context.Context, sub core.Submission) (core.SubmitOutcome, error) {
	first := false
	l.once.Do(func() { first = true })
	if first {
		close(l.before.entered)
		<-l.before.release
	}
	out, err := l.Store.Submit(ctx, sub)
	if err != nil {
		return out, err
	}
	if first {
		return core.SubmitOutcome{}, &core.Failure{Kind: core.FailureConflict, Message: "the reply was lost"}
	}
	return out, nil
}

// Regression: the watcher learned of a change only from a commit that said it worked. A commit that
// landed and failed was announced to nobody until the slow period.
func TestACommitThatFailedAfterLandingStillWakesAWatcher(t *testing.T) {
	w, o := newOutage(t)
	before := newGate()
	o.Stores = failsAfterLanding{Stores: w.stores, once: new(sync.Once), before: before}
	a := o.start(w.supervisors())
	wt := watch(t, a, scopeA)
	converge(t, a, wt, o, scopeA, false)
	failed := make(chan error, 1)
	go func() {
		_, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: callA, Spec: command("hold", "g", ";", "exit", "0")})
		failed <- err
	}()
	before.await(t)
	// The store is made and the watcher has looked at it empty; now the job lands and the reply is lost.
	converge(t, a, wt, o, scopeA, false)
	close(before.release)
	if err := <-failed; err == nil {
		t.Fatal("the submit that lost its reply said it worked")
	}
	v := converge(t, a, wt, o, scopeA, false)
	if len(v.Content.Jobs) != 1 {
		t.Fatalf("the store holds %d jobs after a submit that landed, want 1", len(v.Content.Jobs))
	}
	w.gates.Release("g")
}

// A store that is away is a health fact and not a loss of what was held: the watcher keeps the jobs
// it had, and when the store is back it is told only what changed.
func TestAWatcherKeepsWhatItHadWhileTheStoreIsAway(t *testing.T) {
	w, o := newOutage(t)
	a := o.start(w.supervisors())
	job := submitAs(t, a, callA, "print", "x", ";", "exit", "0")
	o.awaitFinal(t, a, scopeA, job.ID)
	wt := watch(t, a, scopeA)
	converge(t, a, wt, o, scopeA, false)
	before := wt.count()
	o.takeAway(repoA)
	for wt.count() == before {
		o.limits.pass()
		converge(t, a, wt, o, scopeA, true)
	}
	wt.mu.Lock()
	health, _ := wt.held["health"].(core.StoreHealth)
	_, hasJob := wt.held[core.FactKey("job/"+string(job.ID))]
	wt.mu.Unlock()
	if health.Away != core.AwayUnavailable || !hasJob {
		t.Fatalf("while the store is away the watcher holds health %+v and job %v, want unavailable and the job kept", health, hasJob)
	}
	o.bringBack(repoA)
	converge(t, a, wt, o, scopeA, true)
	last := wt.sends[len(wt.sends)-1]
	if last.Reset || len(last.Facts) != 1 || last.Facts[0].Key != "health" {
		t.Fatalf("a store that came back unchanged sends %+v, want only the health", last)
	}
}
