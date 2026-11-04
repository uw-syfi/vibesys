package fakelease_test

import (
	"context"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/fakelease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/leasetest"
)

func world(leasetest.TB) leasetest.World {
	p := &fakelease.Place{}
	return leasetest.World{
		Daemon: func() lease.Leases { return p.Daemon() },
		Crash:  func(l lease.Lease) { l.(*fakelease.Lease).Crash() },
		Lose:   func(l lease.Lease) { l.(*fakelease.Lease).Lose() },
	}
}

func TestTheFakeKeepsTheContract(t *testing.T) {
	leasetest.Run(t, leasetest.Contract{New: world})
}

// TestTheContractCatchesABrokenLease runs the contract against leases broken one way each, and
// sees each one caught.
func TestTheContractCatchesABrokenLease(t *testing.T) {
	for name, broken := range map[string]func(lease.Leases) lease.Leases{
		"grants a held lease":    func(l lease.Leases) lease.Leases { return grantsHeld{l} },
		"keeps a released lease": func(l lease.Leases) lease.Leases { return keepsReleased{l} },
		"names nobody":           func(l lease.Leases) lease.Leases { return namesNobody{l} },
		"outlives its holder":    func(l lease.Leases) lease.Leases { return l },
	} {
		t.Run(name, func(t *testing.T) {
			got := leasetest.Broken(leasetest.Contract{Seeds: 5, New: func(tb leasetest.TB) leasetest.World {
				w := world(tb)
				daemon := w.Daemon
				w.Daemon = func() lease.Leases { return broken(daemon()) }
				crash, lose := w.Crash, w.Lose
				w.Crash = func(l lease.Lease) { crash(inner(l)) }
				if name == "outlives its holder" {
					w.Crash = func(lease.Lease) {}
				}
				w.Lose = func(l lease.Lease) { lose(inner(l)) }
				return w
			}})
			if len(got) == 0 {
				t.Fatalf("the contract passed a lease that %s", name)
			}
			t.Logf("caught by %v", got)
		})
	}
}

// wrapped is a lease a broken Leases hands out, over the real one.
type wrapped struct {
	lease.Lease
	release func(context.Context) error
}

func (w wrapped) Release(ctx context.Context) error { return w.release(ctx) }

func inner(l lease.Lease) lease.Lease {
	if w, ok := l.(wrapped); ok {
		return w.Lease
	}
	return l
}

// grantsHeld hands out a lease that is held already.
type grantsHeld struct{ lease.Leases }

func (g grantsHeld) Acquire(ctx context.Context, k lease.Key, h core.LeaseHolder) (lease.Lease, error) {
	l, err := g.Leases.Acquire(ctx, k, h)
	if err != nil {
		return wrapped{Lease: phantom{k}, release: func(context.Context) error { return nil }}, nil
	}
	return l, nil
}

// keepsReleased does not give a lease back on Release.
type keepsReleased struct{ lease.Leases }

func (k keepsReleased) Acquire(ctx context.Context, key lease.Key, h core.LeaseHolder) (lease.Lease, error) {
	l, err := k.Leases.Acquire(ctx, key, h)
	if err != nil {
		return nil, err
	}
	return wrapped{Lease: l, release: func(context.Context) error { return nil }}, nil
}

// namesNobody refuses a held lease without saying who holds it.
type namesNobody struct{ lease.Leases }

func (n namesNobody) Acquire(ctx context.Context, key lease.Key, h core.LeaseHolder) (lease.Lease, error) {
	l, err := n.Leases.Acquire(ctx, key, h)
	if held, ok := err.(*lease.HeldError); ok {
		return nil, &lease.HeldError{Key: held.Key, Where: held.Where}
	}
	return l, err
}

// phantom is a lease that was never taken.
type phantom struct{ key lease.Key }

func (p phantom) Release(context.Context) error { return nil }
func (phantom) Lost() <-chan struct{}           { return nil }
func (p phantom) Key() lease.Key                { return p.key }
