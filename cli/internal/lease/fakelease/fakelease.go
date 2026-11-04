// Package fakelease keeps leases in memory, for tests and the simulator: a Place stands for one
// place that several daemons share, and each Daemon of it for one of them. A daemon that crashes
// gives back what it holds, as the operating system or an expiry would.
package fakelease

import (
	"context"
	"sort"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
)

// Place is a place where leases are kept. Its zero value holds none.
type Place struct {
	mu   sync.Mutex
	held map[lease.Key]*Lease
	// Name is what Where says; empty says "the fake place".
	Name string
}

// Daemon returns the Leases of one daemon at the place.
func (p *Place) Daemon() *Daemon { return &Daemon{place: p} }

// Held lists the leases held at the place, by key.
func (p *Place) Held() map[lease.Key]core.LeaseHolder {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := map[lease.Key]core.LeaseHolder{}
	for k, l := range p.held {
		out[k] = l.holder
	}
	return out
}

// Daemon is the Leases of one daemon at a Place.
type Daemon struct {
	place *Place
	mu    sync.Mutex
	// held are the leases this daemon holds, so Crash can give them back.
	held []*Lease
}

var _ lease.Leases = (*Daemon)(nil)

// Acquire implements lease.Leases.
func (d *Daemon) Acquire(ctx context.Context, key lease.Key, holder core.LeaseHolder) (lease.Lease, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	p := d.place
	p.mu.Lock()
	defer p.mu.Unlock()
	if cur, ok := p.held[key]; ok {
		return nil, &lease.HeldError{Key: key, Where: d.Where(), Holder: cur.holder}
	}
	if p.held == nil {
		p.held = map[lease.Key]*Lease{}
	}
	l := &Lease{place: p, key: key, holder: holder, lost: make(chan struct{})}
	p.held[key] = l
	d.mu.Lock()
	d.held = append(d.held, l)
	d.mu.Unlock()
	return l, nil
}

// Where implements lease.Leases.
func (d *Daemon) Where() string {
	if d.place.Name != "" {
		return d.place.Name
	}
	return "the fake place"
}

// Crash gives back every lease the daemon holds, as its death would, without a Release; none of
// them says it was lost, since nobody is left to hear it.
func (d *Daemon) Crash() {
	d.mu.Lock()
	held := d.held
	d.held = nil
	d.mu.Unlock()
	for _, l := range held {
		l.drop()
	}
}

// Holding lists the keys the daemon holds, in order.
func (d *Daemon) Holding() []lease.Key {
	d.mu.Lock()
	defer d.mu.Unlock()
	var out []lease.Key
	for _, l := range d.held {
		if l.holds() {
			out = append(out, l.key)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// Lease is a lease held at a Place.
type Lease struct {
	place    *Place
	key      lease.Key
	holder   core.LeaseHolder
	lost     chan struct{}
	lostOnce sync.Once
}

var _ lease.Lease = (*Lease)(nil)

// Release implements lease.Lease.
func (l *Lease) Release(context.Context) error {
	l.drop()
	return nil
}

// Lost implements lease.Lease.
func (l *Lease) Lost() <-chan struct{} { return l.lost }

// Key implements lease.Lease.
func (l *Lease) Key() lease.Key { return l.key }

// Crash gives the lease back as the death of its holder would, without saying it was lost.
func (l *Lease) Crash() { l.drop() }

// Lose gives the lease back as an expiry would, and says it was lost.
func (l *Lease) Lose() {
	l.drop()
	l.lostOnce.Do(func() { close(l.lost) })
}

// drop gives the lease back if it is still the one held.
func (l *Lease) drop() {
	l.place.mu.Lock()
	defer l.place.mu.Unlock()
	if l.place.held[l.key] == l {
		delete(l.place.held, l.key)
	}
}

func (l *Lease) holds() bool {
	l.place.mu.Lock()
	defer l.place.mu.Unlock()
	return l.place.held[l.key] == l
}
