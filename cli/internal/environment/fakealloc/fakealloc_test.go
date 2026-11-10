package fakealloc_test

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/allocatortest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakealloc"
)

var epoch = time.Date(2026, 11, 10, 0, 0, 0, 0, time.UTC)

func world(o fakealloc.Options) func(allocatortest.TB) allocatortest.World {
	return func(allocatortest.TB) allocatortest.World {
		c := clock.NewManual(epoch, 0)
		f := fakealloc.New(c, o)
		planted := 0
		return allocatortest.World{
			Allocator:     f,
			Pass:          c.Advance,
			Endless:       o.Endless,
			Cut:           f.SetUnreachable,
			CancelOutside: f.CancelOutside,
			Foreign: func() {
				planted++
				f.Plant(alloc.Spec{ID: alloc.ID(fmt.Sprintf("a%d", planted)), Run: "someone", Placement: "p", For: time.Hour})
			},
		}
	}
}

// The fake keeps the contract, whether its allocations start at the first look or wait in a queue,
// and whether they have a time limit or none.
func TestTheFakeKeepsTheContract(t *testing.T) {
	for name, o := range map[string]fakealloc.Options{
		"starting at once":    {},
		"waiting in a queue":  {Looks: 5},
		"with no time limits": {Endless: true, Looks: 2},
	} {
		t.Run(name, func(t *testing.T) { allocatortest.Run(t, allocatortest.Contract{New: world(o)}) })
	}
}

// TestTheContractCatchesABrokenAllocator runs the contract against allocators broken one way each,
// and sees each one caught.
func TestTheContractCatchesABrokenAllocator(t *testing.T) {
	for name, broken := range map[string]func(*fakealloc.FakeAllocator) environment.Allocator{
		"adopts what it did not make":     func(f *fakealloc.FakeAllocator) environment.Allocator { return adoptsAll{f} },
		"ends what it cannot reach":       func(f *fakealloc.FakeAllocator) environment.Allocator { return endsUnreachable{f} },
		"fails a second release":          func(f *fakealloc.FakeAllocator) environment.Allocator { return &releasesOnce{FakeAllocator: f} },
		"makes a new one for each ask":    func(f *fakealloc.FakeAllocator) environment.Allocator { return &asksAnew{FakeAllocator: f} },
		"outlives its time limit":         func(f *fakealloc.FakeAllocator) environment.Allocator { return outlives{f} },
		"never starts":                    func(f *fakealloc.FakeAllocator) environment.Allocator { return neverStarts{f} },
		"forgets what it waits on":        func(f *fakealloc.FakeAllocator) environment.Allocator { return forgetsWaiting{f} },
		"takes a request of no time":      func(f *fakealloc.FakeAllocator) environment.Allocator { return takesNoTime{f} },
		"says nothing of a release later": func(f *fakealloc.FakeAllocator) environment.Allocator { return releaseKeepsRunning{f} },
	} {
		t.Run(name, func(t *testing.T) {
			got := allocatortest.Broken(allocatortest.Contract{New: func(tb allocatortest.TB) allocatortest.World {
				w := world(fakealloc.Options{Looks: 1})(tb)
				w.Allocator = broken(w.Allocator.(*fakealloc.FakeAllocator))
				return w
			}})
			if len(got) == 0 {
				t.Fatalf("the contract passed an allocator that %s", name)
			}
			t.Logf("caught by %v", got)
		})
	}
}

// adoptsAll returns every live allocation of the backend, the ones it did not make included.
type adoptsAll struct{ *fakealloc.FakeAllocator }

func (a adoptsAll) Adopt(ctx context.Context) ([]alloc.Handle, error) {
	if _, err := a.FakeAllocator.Adopt(ctx); err != nil {
		return nil, err
	}
	return a.Live(), nil
}

// endsUnreachable says an allocation it cannot ask about has ended.
type endsUnreachable struct{ *fakealloc.FakeAllocator }

func (e endsUnreachable) Observe(ctx context.Context, h alloc.Handle) (alloc.Observation, error) {
	o, err := e.FakeAllocator.Observe(ctx, h)
	if err != nil {
		return alloc.SeenEnded{Cause: alloc.EndedOtherwise}, nil
	}
	return o, nil
}

type releasesOnce struct {
	*fakealloc.FakeAllocator
	released map[alloc.Handle]bool
}

func (r *releasesOnce) Release(ctx context.Context, h alloc.Handle) error {
	if r.released[h] {
		return fmt.Errorf("%s was released already", h.Ref)
	}
	if r.released == nil {
		r.released = map[alloc.Handle]bool{}
	}
	r.released[h] = true
	return r.FakeAllocator.Release(ctx, h)
}

// asksAnew makes a new allocation for every request, as a backend with no mark of the ID would.
type asksAnew struct {
	*fakealloc.FakeAllocator
	n int
}

func (a *asksAnew) Request(ctx context.Context, s alloc.Spec) (alloc.Handle, error) {
	if _, err := a.FakeAllocator.Request(ctx, s); err != nil {
		return alloc.Handle{}, err
	}
	a.n++
	s.ID = alloc.ID(fmt.Sprintf("%s%d", s.ID, 9000+a.n))
	h, err := a.FakeAllocator.Request(ctx, s)
	h.Alloc = s.ID[:len(s.ID)-4]
	return h, err
}

// outlives says an allocation ends later than its time limit.
type outlives struct{ *fakealloc.FakeAllocator }

func (o outlives) Observe(ctx context.Context, h alloc.Handle) (alloc.Observation, error) {
	seen, err := o.FakeAllocator.Observe(ctx, h)
	if r, ok := seen.(alloc.SeenRunning); ok {
		r.EndsAt = r.EndsAt.Add(time.Hour)
		return r, err
	}
	return seen, err
}

type neverStarts struct{ *fakealloc.FakeAllocator }

func (n neverStarts) Observe(ctx context.Context, h alloc.Handle) (alloc.Observation, error) {
	seen, err := n.FakeAllocator.Observe(ctx, h)
	if _, ok := seen.(alloc.SeenRunning); ok {
		return alloc.SeenPending{}, err
	}
	return seen, err
}

// forgetsWaiting leaves the allocations that wait out of Adopt.
type forgetsWaiting struct{ *fakealloc.FakeAllocator }

func (f forgetsWaiting) Adopt(ctx context.Context) ([]alloc.Handle, error) {
	hs, err := f.FakeAllocator.Adopt(ctx)
	if err != nil {
		return nil, err
	}
	var out []alloc.Handle
	for _, h := range hs {
		if _, ok := mustObserve(f.FakeAllocator, h).(alloc.SeenRunning); ok {
			out = append(out, h)
		}
	}
	return out, nil
}

func mustObserve(f *fakealloc.FakeAllocator, h alloc.Handle) alloc.Observation {
	o, err := f.Observe(context.Background(), h)
	if err != nil {
		panic(err)
	}
	return o
}

type takesNoTime struct{ *fakealloc.FakeAllocator }

func (n takesNoTime) Request(ctx context.Context, s alloc.Spec) (alloc.Handle, error) {
	if s.For <= 0 {
		s.For = time.Hour
	}
	if s.ID == "" {
		s.ID = "a99"
	}
	return n.FakeAllocator.Request(ctx, s)
}

// releaseKeepsRunning acknowledges a release and does nothing.
type releaseKeepsRunning struct{ *fakealloc.FakeAllocator }

func (releaseKeepsRunning) Release(context.Context, alloc.Handle) error { return nil }
