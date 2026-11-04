// Package leasetest is the contract every Leases implementation passes: one holder at a time per
// lease, whoever asks; a holder that is named, as it was, to whoever finds the lease held; a lease
// given back by Release on every path, and by the death of its holder without one; and nothing held
// after an attempt that failed.
package leasetest

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
)

// TB is what the contract needs of a test.
type TB interface {
	Helper()
	Fatalf(format string, args ...any)
	Cleanup(func())
	TempDir() string
}

// World is one place where leases are kept, as several daemons see it.
type World struct {
	// Daemon returns the Leases of one more daemon that shares the place with the others. Each
	// call stands for a daemon of its own, such as another process or another machine.
	Daemon func() lease.Leases
	// Crash has the holder of l die without giving it back, as a killed process does, and returns
	// once the place would grant the lease to another: at once where the operating system gives it
	// back, or after the world has let it expire.
	Crash func(l lease.Lease)
	// Lose, when set, breaks what keeps l held, such as the connection to its host or the renewals
	// of its expiry, and returns once l is given back, or once a bound that only guards against a
	// hang has passed. Lost must then be closed.
	Lose func(l lease.Lease)
}

// Contract is an implementation under test.
type Contract struct {
	// New makes a world with no lease held.
	New func(t TB) World
	// Seeds is how many random sequences the model property runs; zero is 20.
	Seeds int
}

// Run checks every property of the contract.
func Run(t *testing.T, c Contract) {
	t.Helper()
	for _, p := range properties() {
		t.Run(p.name, func(t *testing.T) { p.check(t, c) })
	}
}

// Broken returns the first violation of each property the contract finds, by property, and none for
// an implementation that keeps them. A test of the contract itself uses it to see the contract catch
// a broken implementation.
func Broken(c Contract) map[string]string {
	out := map[string]string{}
	for _, p := range properties() {
		r := &recorder{}
		func() {
			defer r.cleanup()
			defer func() {
				if v := recover(); v != nil && v != errStop {
					r.msg = fmt.Sprint("panic: ", v)
				}
			}()
			p.check(r, c)
		}()
		if r.msg != "" {
			out[p.name] = r.msg
		}
	}
	return out
}

type property struct {
	name  string
	check func(TB, Contract)
}

func properties() []property {
	return []property{
		{"OneHolderAtATime", oneHolderAtATime},
		{"TheHolderIsNamed", theHolderIsNamed},
		{"KeysAreApart", keysAreApart},
		{"ReleaseGivesItBack", releaseGivesItBack},
		{"ACrashGivesItBack", aCrashGivesItBack},
		{"AFailedAttemptHoldsNothing", aFailedAttemptHoldsNothing},
		{"ALostLeaseSaysSo", aLostLeaseSaysSo},
		{"ItKeepsTheModel", itKeepsTheModel},
	}
}

// bound guards against a hang only: every call of the contract answers at once, or by expiry the
// world advances itself.
const bound = time.Minute

var epoch = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

// holder is the holder a daemon d takes a lease for job j as.
func holder(d, j int) core.LeaseHolder {
	return core.LeaseHolder{
		User: fmt.Sprintf("user-%d", d), Host: fmt.Sprintf("host-%d", d), PID: 100 + d,
		Job: core.JobID(fmt.Sprintf("job-%d-%d", d, j)), Since: epoch.Add(time.Duration(j) * time.Second),
	}
}

func ctx(t TB) context.Context {
	c, cancel := context.WithTimeout(context.Background(), bound)
	t.Cleanup(cancel)
	return c
}

func acquire(t TB, l lease.Leases, k lease.Key, h core.LeaseHolder) lease.Lease {
	t.Helper()
	got, err := l.Acquire(ctx(t), k, h)
	if err != nil {
		t.Fatalf("%s: acquiring %s as %s: %v", l.Where(), k, h, err)
	}
	t.Cleanup(func() { _ = got.Release(context.Background()) })
	return got
}

// refused checks that acquiring k is refused for being held by want.
func refused(t TB, l lease.Leases, k lease.Key, h core.LeaseHolder, want core.LeaseHolder) {
	t.Helper()
	got, err := l.Acquire(ctx(t), k, h)
	if err == nil {
		_ = got.Release(context.Background())
		t.Fatalf("%s: %s took %s, which %s holds", l.Where(), h, k, want)
	}
	var held *lease.HeldError
	if !errors.As(err, &held) {
		t.Fatalf("%s: %s acquiring %s held by %s: want a *HeldError, got %v", l.Where(), h, k, want, err)
	}
	if !sameHolder(held.Holder, want) {
		t.Fatalf("%s: %s is held by %s, and the error names %s", l.Where(), k, want, held.Holder)
	}
	if held.Key != k {
		t.Fatalf("%s: the error for %s names the lease %s", l.Where(), k, held.Key)
	}
}

// sameHolder compares holders, their times to the second, which every place keeps.
func sameHolder(a, b core.LeaseHolder) bool {
	return a.User == b.User && a.Host == b.Host && a.PID == b.PID && a.Job == b.Job &&
		a.Since.Truncate(time.Second).Equal(b.Since.Truncate(time.Second))
}

func oneHolderAtATime(t TB, c Contract) {
	w := c.New(t)
	a, b := w.Daemon(), w.Daemon()
	acquire(t, a, "k", holder(0, 1))
	refused(t, b, "k", holder(1, 1), holder(0, 1))
	// The daemon that holds it is refused too, for another job: a lease is held by one job.
	refused(t, a, "k", holder(0, 2), holder(0, 1))
}

func theHolderIsNamed(t TB, c Contract) {
	w := c.New(t)
	a, b := w.Daemon(), w.Daemon()
	h := core.LeaseHolder{User: "alice", Host: "host-a", PID: 4242, Job: "0198f1b2-job", Since: epoch.Add(90 * time.Minute)}
	acquire(t, a, "named", h)
	refused(t, b, "named", holder(1, 1), h)
}

func keysAreApart(t TB, c Contract) {
	w := c.New(t)
	a, b := w.Daemon(), w.Daemon()
	acquire(t, a, "k1", holder(0, 1))
	acquire(t, b, "k2", holder(1, 1))
	refused(t, b, "k1", holder(1, 2), holder(0, 1))
	refused(t, a, "k2", holder(0, 2), holder(1, 1))
}

func releaseGivesItBack(t TB, c Contract) {
	w := c.New(t)
	a, b := w.Daemon(), w.Daemon()
	l := acquire(t, a, "k", holder(0, 1))
	if l.Key() != "k" {
		t.Fatalf("the lease of k says its key is %s", l.Key())
	}
	if err := l.Release(ctx(t)); err != nil {
		t.Fatalf("releasing: %v", err)
	}
	if err := l.Release(ctx(t)); err != nil {
		t.Fatalf("releasing a second time: %v", err)
	}
	next := acquire(t, b, "k", holder(1, 1))
	// A Release of an old lease does not give back the one taken after it.
	if err := l.Release(ctx(t)); err != nil {
		t.Fatalf("releasing a third time: %v", err)
	}
	refused(t, a, "k", holder(0, 2), holder(1, 1))
	if err := next.Release(ctx(t)); err != nil {
		t.Fatalf("releasing: %v", err)
	}
	acquire(t, a, "k", holder(0, 3))
	select {
	case <-l.Lost():
		t.Fatalf("a released lease says it was lost")
	default:
	}
}

func aCrashGivesItBack(t TB, c Contract) {
	w := c.New(t)
	a, b := w.Daemon(), w.Daemon()
	l := acquire(t, a, "k", holder(0, 1))
	w.Crash(l)
	acquire(t, b, "k", holder(1, 1))
}

func aFailedAttemptHoldsNothing(t TB, c Contract) {
	w := c.New(t)
	a, b := w.Daemon(), w.Daemon()
	done, cancel := context.WithCancel(context.Background())
	cancel()
	if l, err := a.Acquire(done, "k", holder(0, 1)); err == nil {
		// Taking a lease with no time left may succeed where it needs no wait; then it is held.
		refused(t, b, "k", holder(1, 1), holder(0, 1))
		if err := l.Release(ctx(t)); err != nil {
			t.Fatalf("releasing: %v", err)
		}
	}
	acquire(t, b, "k", holder(1, 2))
	// A refusal holds nothing either: the holder gives it back and the one refused then takes it.
	w2 := c.New(t)
	x, y := w2.Daemon(), w2.Daemon()
	l := acquire(t, x, "k", holder(0, 1))
	refused(t, y, "k", holder(1, 1), holder(0, 1))
	if err := l.Release(ctx(t)); err != nil {
		t.Fatalf("releasing: %v", err)
	}
	acquire(t, y, "k", holder(1, 2))
}

func aLostLeaseSaysSo(t TB, c Contract) {
	w := c.New(t)
	if w.Lose == nil {
		return
	}
	a, b := w.Daemon(), w.Daemon()
	l := acquire(t, a, "k", holder(0, 1))
	w.Lose(l)
	select {
	case <-l.Lost():
	default:
		t.Fatalf("a lost lease did not say so")
	}
	acquire(t, b, "k", holder(1, 1))
	if err := l.Release(ctx(t)); err != nil {
		t.Fatalf("releasing a lost lease: %v", err)
	}
	refused(t, a, "k", holder(0, 2), holder(1, 1))
}

// itKeepsTheModel runs random acquires, releases and crashes of three daemons on two keys, and
// checks each answer against a model in which a key is free or held by one holder.
func itKeepsTheModel(t TB, c Contract) {
	seeds := c.Seeds
	if seeds == 0 {
		seeds = 20
	}
	keys := []lease.Key{"a", "b"}
	for seed := range seeds {
		r := rand.New(rand.NewPCG(uint64(seed), 0x1ea5e))
		w := c.New(t)
		daemons := []lease.Leases{w.Daemon(), w.Daemon(), w.Daemon()}
		type held struct {
			l lease.Lease
			h core.LeaseHolder
		}
		model := map[lease.Key]*held{}
		var old []lease.Lease
		for step := range 30 {
			k := keys[r.IntN(len(keys))]
			d := r.IntN(len(daemons))
			h := holder(d, step)
			cur := model[k]
			switch op := r.IntN(10); {
			case op < 6:
				got, err := daemons[d].Acquire(ctx(t), k, h)
				switch {
				case cur == nil && err != nil:
					t.Fatalf("seed %d step %d: %s is free and %s was refused it: %v", seed, step, k, h, err)
				case cur == nil:
					model[k] = &held{got, h}
					t.Cleanup(func() { _ = got.Release(context.Background()) })
				case err == nil:
					t.Fatalf("seed %d step %d: %s took %s, which %s holds", seed, step, h, k, cur.h)
				default:
					var e *lease.HeldError
					if !errors.As(err, &e) || !sameHolder(e.Holder, cur.h) {
						t.Fatalf("seed %d step %d: %s is held by %s, and %s was told %v", seed, step, k, cur.h, h, err)
					}
				}
			case op < 8 && cur != nil:
				if err := cur.l.Release(ctx(t)); err != nil {
					t.Fatalf("seed %d step %d: releasing %s: %v", seed, step, k, err)
				}
				old = append(old, cur.l)
				delete(model, k)
			case op < 9 && cur != nil:
				// A crashed holder calls nothing after: its lease is not released again.
				w.Crash(cur.l)
				delete(model, k)
			case len(old) > 0:
				// Releasing a lease again, after it was given back, gives back nothing taken since.
				if err := old[r.IntN(len(old))].Release(ctx(t)); err != nil {
					t.Fatalf("seed %d step %d: releasing an old lease: %v", seed, step, err)
				}
			}
		}
		for _, k := range slices.Sorted(func(yield func(lease.Key) bool) {
			for k := range model {
				if !yield(k) {
					return
				}
			}
		}) {
			_ = model[k].l.Release(ctx(t))
		}
	}
}

var errStop = &struct{ msg string }{"stop"}

// recorder is a TB that keeps the first failure and stops the property there.
type recorder struct {
	msg      string
	cleanups []func()
}

func (*recorder) Helper() {}

func (r *recorder) Fatalf(format string, args ...any) {
	r.msg = fmt.Sprintf(format, args...)
	panic(errStop)
}

func (r *recorder) Cleanup(f func()) { r.cleanups = append(r.cleanups, f) }

func (r *recorder) TempDir() string {
	dir, err := os.MkdirTemp("", "leasetest")
	if err != nil {
		panic(err)
	}
	r.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
}

func (r *recorder) cleanup() {
	for i := len(r.cleanups) - 1; i >= 0; i-- {
		r.cleanups[i]()
	}
}
