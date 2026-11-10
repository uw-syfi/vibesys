// Package allocatortest is the contract every environment.Allocator passes: Request returns a handle
// at once, and the same one for the same ID; an allocation starts once the backend lets it, within
// its time limit; Release ends it and may be repeated; Adopt finds exactly the live standing
// allocations the allocator made; and a backend that cannot be reached is a failure of the
// unavailable kind, never an end.
package allocatortest

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// TB is what the contract needs of a test.
type TB interface {
	Helper()
	Fatalf(format string, args ...any)
	Cleanup(func())
	Context() context.Context
}

// World is one backend and the allocator under test, with the levers the contract pulls.
type World struct {
	Allocator environment.Allocator
	// Pass lets the backend's time move on by d, as it does between two looks: queued allocations
	// may start, and running ones reach their time limit.
	Pass func(d time.Duration)
	// Endless says the backend's allocations have no time limit, such as a slot of a machine.
	Endless bool
	// Cut, when set, makes the backend unreachable (true), or reachable again (false).
	Cut func(unreachable bool)
	// CancelOutside, when set, ends the allocation as someone outside the CLI would.
	CancelOutside func(h alloc.Handle)
	// Foreign, when set, makes an allocation on the backend that the allocator did not make, such as
	// a job of the user's own, or one a job without a standing allocation runs in.
	Foreign func()
}

// Contract is an implementation under test.
type Contract struct {
	// New makes a world whose backend holds nothing.
	New func(t TB) World
	// Looks is how many looks, with a Pass of Step before each, an allocation gets to start; zero
	// is 20. It only bounds a backend that never starts one.
	Looks int
	// Step is how far each Pass moves time while the contract waits for a start; zero is a minute.
	Step time.Duration
}

// Run checks every property of the contract.
func Run(t *testing.T, c Contract) {
	t.Helper()
	for _, p := range properties() {
		t.Run(p.name, func(t *testing.T) { p.check(t, c) })
	}
}

// Broken returns the first violation of each property the contract finds, by property, and none
// for an implementation that keeps them. A test of the contract itself uses it to see the contract
// catch a broken implementation.
func Broken(c Contract) map[string]string {
	out := map[string]string{}
	for _, p := range properties() {
		r := &recorder{ctx: context.Background()}
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
		{"RequestReturnsAHandleAtOnce", requestReturnsAHandleAtOnce},
		{"RequestIsIdempotentByID", requestIsIdempotentByID},
		{"AnInvalidSpecIsRefused", anInvalidSpecIsRefused},
		{"ItStartsWithinItsTimeLimit", itStartsWithinItsTimeLimit},
		{"ReleaseEndsItAndMayBeRepeated", releaseEndsItAndMayBeRepeated},
		{"AdoptFindsExactlyTheLiveOnesItMade", adoptFindsExactlyTheLiveOnesItMade},
		{"UnreachableIsUnavailableNotAnEnd", unreachableIsUnavailableNotAnEnd},
		{"TheTimeLimitEndsIt", theTimeLimitEndsIt},
		{"ACancelOutsideEndsIt", aCancelOutsideEndsIt},
	}
}

func (c Contract) looks() int {
	if c.Looks > 0 {
		return c.Looks
	}
	return 20
}

func (c Contract) step() time.Duration {
	if c.Step > 0 {
		return c.Step
	}
	return time.Minute
}

var specFor = 2 * time.Hour

func spec(n int) alloc.Spec {
	return alloc.Spec{ID: alloc.ID(fmt.Sprintf("a%d", n)), Run: "run-a", Placement: "p", For: specFor}
}

func request(t TB, w World, s alloc.Spec) alloc.Handle {
	t.Helper()
	h, err := w.Allocator.Request(t.Context(), s)
	if err != nil {
		t.Fatalf("Request(%+v): %v", s, err)
	}
	if h.Alloc != s.ID || h.Ref == "" {
		t.Fatalf("Request(%+v) = %+v, want a handle of %s with the backend's name for it", s, h, s.ID)
	}
	return h
}

func observe(t TB, w World, h alloc.Handle) alloc.Observation {
	t.Helper()
	o, err := w.Allocator.Observe(t.Context(), h)
	if err != nil {
		t.Fatalf("Observe(%+v): %v", h, err)
	}
	if o == nil {
		t.Fatalf("Observe(%+v) said nothing", h)
	}
	return o
}

// started looks until the allocation runs, and fails when it never does or ends instead.
func started(t TB, c Contract, w World, h alloc.Handle) alloc.SeenRunning {
	t.Helper()
	for range c.looks() {
		switch o := observe(t, w, h).(type) {
		case alloc.SeenRunning:
			return o
		case alloc.SeenEnded:
			t.Fatalf("%+v ended (%+v) before it started", h, o)
		case alloc.SeenPending:
		}
		w.Pass(c.step())
	}
	t.Fatalf("%+v did not start in %d looks %s apart", h, c.looks(), c.step())
	return alloc.SeenRunning{}
}

func adopted(t TB, w World) []alloc.Handle {
	t.Helper()
	hs, err := w.Allocator.Adopt(t.Context())
	if err != nil {
		t.Fatalf("Adopt: %v", err)
	}
	slices.SortFunc(hs, func(a, b alloc.Handle) int {
		if a.Alloc != b.Alloc {
			return compareStrings(string(a.Alloc), string(b.Alloc))
		}
		return compareStrings(a.Ref, b.Ref)
	})
	return hs
}

func compareStrings(a, b string) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}

func ended(t TB, w World, h alloc.Handle) alloc.SeenEnded {
	t.Helper()
	o, ok := observe(t, w, h).(alloc.SeenEnded)
	if !ok {
		t.Fatalf("%+v is not seen ended", h)
	}
	return o
}

func unavailable(err error) bool {
	var f *core.Failure
	return errors.As(err, &f) && f.Kind == core.FailureUnavailable
}

// requestReturnsAHandleAtOnce: the request returns a handle before the allocation runs, and the
// backend knows it at once.
func requestReturnsAHandleAtOnce(t TB, c Contract) {
	w := c.New(t)
	h := request(t, w, spec(1))
	if _, ok := observe(t, w, h).(alloc.SeenEnded); ok {
		t.Fatalf("%+v is seen ended right after the request", h)
	}
}

// requestIsIdempotentByID: asking again for the same ID is the same allocation, and nothing more is
// made, whether it waits or runs.
func requestIsIdempotentByID(t TB, c Contract) {
	w := c.New(t)
	first := request(t, w, spec(1))
	if again := request(t, w, spec(1)); again != first {
		t.Fatalf("a second Request of a1 = %+v, want the first, %+v", again, first)
	}
	started(t, c, w, first)
	if again := request(t, w, spec(1)); again != first {
		t.Fatalf("a Request of a1 once it ran = %+v, want the first, %+v", again, first)
	}
	if got := adopted(t, w); !slices.Equal(got, []alloc.Handle{first}) {
		t.Fatalf("Adopt = %+v after three requests of one ID, want %+v", got, first)
	}
}

// anInvalidSpecIsRefused: an allocation of no ID or of no time is refused as invalid input, and
// nothing is made.
func anInvalidSpecIsRefused(t TB, c Contract) {
	w := c.New(t)
	for _, s := range []alloc.Spec{
		{Run: "run-a", Placement: "p", For: time.Hour},
		{ID: "a1", Run: "run-a", Placement: "p"},
		{ID: "a1", Run: "run-a", Placement: "p", For: -time.Hour},
	} {
		_, err := w.Allocator.Request(t.Context(), s)
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Fatalf("Request(%+v) = %v, want an invalid input", s, err)
		}
	}
	if got := adopted(t, w); len(got) != 0 {
		t.Fatalf("Adopt = %+v after only refused requests, want none", got)
	}
}

// itStartsWithinItsTimeLimit: an allocation starts once the backend lets it, and ends no later than
// its time limit from its start, or has no end where the backend has none.
func itStartsWithinItsTimeLimit(t TB, c Contract) {
	w := c.New(t)
	h := request(t, w, spec(1))
	r := started(t, c, w, h)
	switch {
	case w.Endless && !r.EndsAt.IsZero():
		t.Fatalf("%+v of an endless backend ends at %s", h, r.EndsAt)
	case !w.Endless && r.EndsAt.IsZero():
		t.Fatalf("%+v runs with no end, and the backend has time limits", h)
	case !w.Endless && r.EndsAt.After(r.StartedAt.Add(specFor)):
		t.Fatalf("%+v started at %s ends at %s, after its time limit of %s", h, r.StartedAt, r.EndsAt, specFor)
	}
}

// releaseEndsItAndMayBeRepeated: a released allocation is seen ended, whether it waited or ran, and
// releasing it again succeeds.
func releaseEndsItAndMayBeRepeated(t TB, c Contract) {
	w := c.New(t)
	waiting := request(t, w, spec(1))
	running := request(t, w, spec(2))
	started(t, c, w, running)
	for _, h := range []alloc.Handle{waiting, running} {
		if err := w.Allocator.Release(t.Context(), h); err != nil {
			t.Fatalf("Release(%+v): %v", h, err)
		}
		ended(t, w, h)
		if err := w.Allocator.Release(t.Context(), h); err != nil {
			t.Fatalf("a second Release(%+v): %v", h, err)
		}
		ended(t, w, h)
	}
	if got := adopted(t, w); len(got) != 0 {
		t.Fatalf("Adopt = %+v once every allocation was released, want none", got)
	}
}

// adoptFindsExactlyTheLiveOnesItMade: Adopt returns the allocations the allocator made and that have
// not ended, waiting or running, and none of the backend's others.
func adoptFindsExactlyTheLiveOnesItMade(t TB, c Contract) {
	w := c.New(t)
	if w.Foreign != nil {
		w.Foreign()
	}
	waiting := request(t, w, spec(1))
	running := request(t, w, spec(2))
	released := request(t, w, spec(3))
	started(t, c, w, running)
	if w.Foreign != nil {
		w.Foreign()
	}
	if err := w.Allocator.Release(t.Context(), released); err != nil {
		t.Fatalf("Release: %v", err)
	}
	want := []alloc.Handle{waiting, running}
	if got := adopted(t, w); !slices.Equal(got, want) {
		t.Fatalf("Adopt = %+v, want exactly %+v", got, want)
	}
}

// unreachableIsUnavailableNotAnEnd: while the backend cannot be reached every method fails as
// unavailable, and once it can be again the allocation is as it was.
func unreachableIsUnavailableNotAnEnd(t TB, c Contract) {
	w := c.New(t)
	if w.Cut == nil {
		return
	}
	h := request(t, w, spec(1))
	started(t, c, w, h)
	w.Cut(true)
	if _, err := w.Allocator.Observe(t.Context(), h); !unavailable(err) {
		t.Fatalf("Observe of an unreachable backend: %v, want unavailable", err)
	}
	if _, err := w.Allocator.Request(t.Context(), spec(2)); !unavailable(err) {
		t.Fatalf("Request of an unreachable backend: %v, want unavailable", err)
	}
	if err := w.Allocator.Release(t.Context(), h); !unavailable(err) {
		t.Fatalf("Release of an unreachable backend: %v, want unavailable", err)
	}
	if _, err := w.Allocator.Adopt(t.Context()); !unavailable(err) {
		t.Fatalf("Adopt of an unreachable backend: %v, want unavailable", err)
	}
	w.Cut(false)
	if _, ok := observe(t, w, h).(alloc.SeenRunning); !ok {
		t.Fatalf("%+v is not running once the backend is reachable again", h)
	}
	if got := adopted(t, w); !slices.Equal(got, []alloc.Handle{h}) {
		t.Fatalf("Adopt = %+v once the backend is reachable again, want %+v", got, h)
	}
}

// theTimeLimitEndsIt: a running allocation ends by its time limit, and says so.
func theTimeLimitEndsIt(t TB, c Contract) {
	w := c.New(t)
	if w.Endless {
		return
	}
	h := request(t, w, spec(1))
	started(t, c, w, h)
	w.Pass(specFor + c.step())
	if o := ended(t, w, h); o.Cause != alloc.EndedByTimeLimit {
		t.Fatalf("%+v ended %+v after its time limit, want by the time limit", h, o)
	}
	if got := adopted(t, w); len(got) != 0 {
		t.Fatalf("Adopt = %+v after the time limit, want none", got)
	}
}

// aCancelOutsideEndsIt: an allocation cancelled outside the CLI is seen ended, not by its limit.
func aCancelOutsideEndsIt(t TB, c Contract) {
	w := c.New(t)
	if w.CancelOutside == nil {
		return
	}
	h := request(t, w, spec(1))
	started(t, c, w, h)
	w.CancelOutside(h)
	if o := ended(t, w, h); o.Cause != alloc.EndedOtherwise {
		t.Fatalf("%+v cancelled outside the CLI ended %+v, want otherwise", h, o)
	}
}

var errStop = errors.New("allocatortest: stop")

// recorder is the TB of Broken: it keeps the first violation and stops the property there.
type recorder struct {
	msg      string
	cleanups []func()
	ctx      context.Context
}

func (*recorder) Helper() {}

func (r *recorder) Fatalf(format string, args ...any) {
	r.msg = fmt.Sprintf(format, args...)
	panic(errStop)
}

func (r *recorder) Cleanup(f func()) { r.cleanups = append(r.cleanups, f) }

func (r *recorder) Context() context.Context { return r.ctx }

func (r *recorder) cleanup() {
	for i := len(r.cleanups) - 1; i >= 0; i-- {
		r.cleanups[i]()
	}
}
