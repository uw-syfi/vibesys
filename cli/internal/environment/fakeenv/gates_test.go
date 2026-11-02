package fakeenv_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
)

// backstop only guards against a hang: a release that was forgotten waits for it.
const backstop = time.Minute

// kinds are the gates a test can hold, each with a second view of the same gates, which is what
// another process has.
var kinds = []struct {
	name string
	make func(t *testing.T) (fakeenv.Gates, fakeenv.Gates, func(string) error)
}{
	{"memory", func(*testing.T) (fakeenv.Gates, fakeenv.Gates, func(string) error) {
		g := fakeenv.NewMemoryGates()
		return g, g, func(name string) error { g.Release(name); return nil }
	}},
	{"fifo", func(t *testing.T) (fakeenv.Gates, fakeenv.Gates, func(string) error) {
		dir := t.TempDir()
		g := fakeenv.NewFIFOGates(dir)
		return g, fakeenv.NewFIFOGates(dir), g.Release
	}},
}

// A release is never forgotten, whoever gets there first. Every waiter passes, those that came
// before the release and those that came after it, in any order.
func TestAReleaseReachesEveryWaiterInAnyOrder(t *testing.T) {
	for _, kind := range kinds {
		t.Run(kind.name, func(t *testing.T) {
			for seed := range uint64(40) {
				rng := rand.New(rand.NewPCG(seed, 1))
				first, second, release := kind.make(t)
				ctx, cancel := context.WithTimeout(context.Background(), backstop)
				defer cancel()
				gate := fmt.Sprintf("g%d", seed)
				before, after := rng.IntN(4), rng.IntN(4)
				var wg sync.WaitGroup
				wait := func(g fakeenv.Gates) {
					wg.Go(func() {
						if !g.Wait(ctx, gate) {
							t.Errorf("seed %d: a waiter never passed", seed)
						}
					})
				}
				for i := range before {
					wait([]fakeenv.Gates{first, second}[i%2])
				}
				if err := release(gate); err != nil {
					t.Fatal(err)
				}
				for i := range after {
					wait([]fakeenv.Gates{first, second}[i%2])
				}
				wg.Wait()
			}
		})
	}
}

// With nobody there at all, the release lasts until someone comes.
func TestAReleaseWithNoWaiterLasts(t *testing.T) {
	for _, kind := range kinds {
		t.Run(kind.name, func(t *testing.T) {
			_, later, release := kind.make(t)
			if err := release("g"); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), backstop)
			defer cancel()
			if !later.Wait(ctx, "g") {
				t.Fatal("the release was forgotten")
			}
		})
	}
}

func TestAClosedGateHoldsUntilTheContextEnds(t *testing.T) {
	for _, kind := range kinds {
		t.Run(kind.name, func(t *testing.T) {
			first, _, _ := kind.make(t)
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if first.Wait(ctx, "g") {
				t.Fatal("a closed gate let a waiter pass")
			}
		})
	}
}
