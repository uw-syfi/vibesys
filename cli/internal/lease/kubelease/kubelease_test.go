package kubelease_test

import (
	"context"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/kubelease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/kubelease/kubefake"
	"github.com/uw-syfi/vibesys/cli/internal/lease/leasetest"
)

const duration = 30 * time.Second

// world is a fake cluster. A crash expires the one lease of the crashed holder, as the time since
// its last renewal would, and a loss has the cluster take the lease from its holder, which hears of
// it at its next renewal; TestARenewalThatCannotReachTheClusterLosesTheLease covers a loss by
// expiry.
func world(leasetest.TB) leasetest.World {
	server := &kubefake.Server{}
	now := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), 0)
	timers := map[*kubelease.Lease]*clock.ManualTimers{}
	return leasetest.World{
		Daemon: func() lease.Leases {
			t := clock.NewManualTimers()
			return tracking{kubelease.Cluster{API: server, Duration: duration, Clock: now, Timers: t}, t, timers}
		},
		Plant: func(l lease.Lease, record []byte) {
			name := "vibesys-" + string(l.Key())
			o, err := server.Get(context.Background(), name)
			if err != nil || o == nil {
				panic("planting a record: no lease object")
			}
			o.Metadata.Annotations[kubelease.HolderAnnotation] = string(record)
			if err := server.Replace(context.Background(), o); err != nil {
				panic("planting a record: " + err.Error())
			}
		},
		Crash: func(l lease.Lease) {
			l.(*kubelease.Lease).Abandon()
			server.Expire("vibesys-" + string(l.Key()))
		},
		Lose: func(l lease.Lease) {
			k := l.(*kubelease.Lease)
			server.Expire("vibesys-" + string(l.Key()))
			server.Clear("vibesys-" + string(l.Key()))
			t := timers[k]
			// The bound only guards against a renewal that never says the lease was lost.
			hang, done := context.WithTimeout(context.Background(), time.Minute)
			defer done()
			for {
				select {
				case <-k.Lost():
					return
				case <-t.Armed():
					t.Fire()
				case <-hang.Done():
					return
				}
			}
		},
	}
}

// tracking notes the timers of each lease a daemon takes, so Lose can fire its renewals.
type tracking struct {
	kubelease.Cluster
	timers *clock.ManualTimers
	of     map[*kubelease.Lease]*clock.ManualTimers
}

func (t tracking) Acquire(ctx context.Context, k lease.Key, h core.LeaseHolder) (lease.Lease, error) {
	l, err := t.Cluster.Acquire(ctx, k, h)
	if err == nil {
		t.of[l.(*kubelease.Lease)] = t.timers
	}
	return l, err
}

func TestTheLeasesOfAClusterKeepTheContract(t *testing.T) {
	leasetest.Run(t, leasetest.Contract{New: world})
}

// TestARenewedLeaseOutlivesItsDuration has a holder renew its lease past its first duration, so
// another daemon is still refused it.
func TestARenewedLeaseOutlivesItsDuration(t *testing.T) {
	server := &kubefake.Server{}
	now := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), 0)
	timers := clock.NewManualTimers()
	a := kubelease.Cluster{API: server, Duration: duration, Clock: now, Timers: timers}
	b := kubelease.Cluster{API: server, Duration: duration, Clock: now, Timers: clock.NewManualTimers()}
	ctx := context.Background()
	l, err := a.Acquire(ctx, "gpu", core.LeaseHolder{User: "a"})
	if err != nil {
		t.Fatal(err)
	}
	defer l.Release(ctx)
	for range 6 {
		if d := armed(t, timers); d != duration/3 {
			t.Fatalf("the renewal waits %s, want a third of the duration", d)
		}
		now.Advance(duration / 3)
		timers.Fire()
	}
	armed(t, timers)
	if _, err := b.Acquire(ctx, "gpu", core.LeaseHolder{User: "b"}); err == nil {
		t.Fatalf("a lease renewed for %s was taken", 2*duration)
	}
	select {
	case <-l.Lost():
		t.Fatalf("a renewed lease says it was lost")
	default:
	}
}

// TestARenewalThatCannotReachTheClusterLosesTheLease has the cluster stop answering: the holder
// says its lease is lost once a duration has passed since its last renewal, and not before.
func TestARenewalThatCannotReachTheClusterLosesTheLease(t *testing.T) {
	server := &kubefake.Server{}
	now := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), 0)
	timers := clock.NewManualTimers()
	c := kubelease.Cluster{API: server, Duration: duration, Clock: now, Timers: timers}
	ctx := context.Background()
	l, err := c.Acquire(ctx, "gpu", core.LeaseHolder{User: "a"})
	if err != nil {
		t.Fatal(err)
	}
	defer l.Release(ctx)
	server.Down(true)
	for i := 1; i <= 3; i++ {
		armed(t, timers)
		now.Advance(duration / 3)
		select {
		case <-l.Lost():
			t.Fatalf("the lease was lost %s after its last renewal", time.Duration(i-1)*duration/3)
		default:
		}
		timers.Fire()
	}
	// The bound only guards against a renewal that never says the lease was lost.
	hang, done := context.WithTimeout(context.Background(), time.Minute)
	defer done()
	select {
	case <-l.Lost():
	case <-hang.Done():
		t.Fatalf("a lease whose renewals failed for its whole duration was not lost")
	}
}

// TestTwoTakersOfAnExpiredLeaseCannotBothHaveIt has two daemons find a lease expired from the same
// read: the write of the second names a version that is gone, and it is refused.
func TestTwoTakersOfAnExpiredLeaseCannotBothHaveIt(t *testing.T) {
	server := &kubefake.Server{}
	now := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), 0)
	c := kubelease.Cluster{API: &racing{Server: server, n: 2}, Duration: duration, Clock: now, Timers: clock.NewManualTimers()}
	ctx := context.Background()
	first, err := c.Acquire(ctx, "gpu", core.LeaseHolder{User: "first"})
	if err != nil {
		t.Fatal(err)
	}
	first.(*kubelease.Lease).Abandon()
	now.Advance(duration + time.Second)
	got, err := c.Acquire(ctx, "gpu", core.LeaseHolder{User: "second"})
	if err != nil {
		t.Fatal(err)
	}
	defer got.Release(ctx)
	o, _ := server.Get(ctx, "vibesys-gpu")
	if o.Metadata.Annotations[kubelease.HolderAnnotation] == "" {
		t.Fatalf("the lease names no holder")
	}
}

// racing is an API whose Replace is first beaten by another writer n times.
type racing struct {
	*kubefake.Server
	n int
}

func (r *racing) Replace(ctx context.Context, o *kubelease.Object) error {
	if r.n > 0 {
		r.n--
		cur, _ := r.Server.Get(ctx, o.Metadata.Name)
		if cur != nil && cur.Spec.HolderIdentity != o.Spec.HolderIdentity {
			// Someone else writes it first, without taking it.
			_ = r.Server.Replace(ctx, cur)
		}
	}
	return r.Server.Replace(ctx, o)
}

// armed waits for the next renewal to be armed and returns its wait. The bound only guards against
// a lease that stops renewing.
func armed(t *testing.T, timers *clock.ManualTimers) time.Duration {
	t.Helper()
	hang, done := context.WithTimeout(context.Background(), time.Minute)
	defer done()
	select {
	case d := <-timers.Armed():
		return d
	case <-hang.Done():
		t.Fatalf("the lease armed no renewal")
		return 0
	}
}
