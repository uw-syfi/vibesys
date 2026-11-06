package check

import (
	"hash/maphash"
	"math"
	"slices"
	"time"
)

// Window is a bounded-staleness item whose state does not depend on the order in which the
// search linearized concurrent writes, so states reached by different interleavings compare
// equal and the search's cache merges them (a Register remembers the order of every write in
// the last bound, and an exhaustive search over a hot item then explodes).
//
// It relaxes Register soundly: a read that started at c may show any written value that is not
// certainly overwritten in real time, i.e. there is no write that started after it returned and
// itself returned more than bound before c. Every value a Register allows is allowed here (a
// write that real time puts after another is after it in every linearization), so a valid
// history stays valid; a violation that needs the linearization order of overlapping writes to
// show can be missed. Last-writer-wins items with stale reads only (profile fields, prices).
type Window[V comparable] struct {
	es []wentry[V] // sorted by (call, ret); the initial value has call = ret = MinInt64
}

type wentry[V comparable] struct {
	v         V
	call, ret int64
}

// NewWindow starts at the initial value.
func NewWindow[V comparable](initial V) Window[V] {
	return Window[V]{es: []wentry[V]{{initial, math.MinInt64, math.MinInt64}}}
}

// Write records a write over [call, ret] (ret = math.MaxInt64 when its outcome is unknown).
func (w Window[V]) Write(v V, call, ret int64) Window[V] {
	e := wentry[V]{v, call, ret}
	i, _ := slices.BinarySearchFunc(w.es, e, func(a, b wentry[V]) int {
		if a.call != b.call {
			return cmpInt(a.call, b.call)
		}
		return cmpInt(a.ret, b.ret)
	})
	es := make([]wentry[V], 0, len(w.es)+1)
	es = append(es, w.es[:i]...)
	es = append(es, e)
	es = append(es, w.es[i:]...)
	return Window[V]{es: es}
}

func cmpInt(a, b int64) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}

// overwritten says whether entry i is certainly overwritten for a read that starts at c.
func (w Window[V]) overwritten(i int, c int64, bound time.Duration) bool {
	t := before(c, bound)
	ri := w.es[i].ret
	for _, e := range w.es {
		if e.call > ri && e.ret < t {
			return true
		}
	}
	return false
}

// MayShow says whether a read that started at c may show v.
func (w Window[V]) MayShow(v V, c int64, bound time.Duration) bool {
	for i, e := range w.es {
		if e.v == v && !w.overwritten(i, c, bound) {
			return true
		}
	}
	return false
}

// Allowed lists the values a read that started at c may show.
func (w Window[V]) Allowed(c int64, bound time.Duration) []V {
	var out []V
	for i, e := range w.es {
		if !w.overwritten(i, c, bound) && !slices.Contains(out, e.v) {
			out = append(out, e.v)
		}
	}
	return out
}

// Prune drops entries that no read starting at or after floor can show.
func (w Window[V]) Prune(floor int64, bound time.Duration) Window[V] {
	var keep []wentry[V]
	dropped := false
	for i, e := range w.es {
		if w.overwritten(i, floor, bound) {
			dropped = true
			continue
		}
		keep = append(keep, e)
	}
	if !dropped {
		return w
	}
	return Window[V]{es: keep}
}

// Len is the number of entries kept.
func (w Window[V]) Len() int { return len(w.es) }

// Equal compares entries.
func (w Window[V]) Equal(o Window[V]) bool { return slices.Equal(w.es, o.es) }

// Hash agrees with Equal.
func (w Window[V]) Hash() uint64 {
	var h maphash.Hash
	h.SetSeed(seed)
	for _, e := range w.es {
		maphash.WriteComparable(&h, e)
	}
	return h.Sum64()
}

// Counter is a commutative counter under bounded staleness: increments (the units of a resource
// taken) whose sum is exact for strict operations and whose recent increments make a stale read's
// range. Its state is the settled sum plus the set of recent increments, independent of the
// order the search applied them in.
type Counter struct {
	settled int
	recent  []cinc // sorted
}

type cinc struct {
	n         int
	call, ret int64
}

// Add records an increment over [call, ret].
func (c Counter) Add(n int, call, ret int64) Counter {
	e := cinc{n, call, ret}
	i, _ := slices.BinarySearchFunc(c.recent, e, func(a, b cinc) int {
		if a.call != b.call {
			return cmpInt(a.call, b.call)
		}
		if a.ret != b.ret {
			return cmpInt(a.ret, b.ret)
		}
		return a.n - b.n
	})
	r := make([]cinc, 0, len(c.recent)+1)
	r = append(r, c.recent[:i]...)
	r = append(r, e)
	r = append(r, c.recent[i:]...)
	return Counter{settled: c.settled, recent: r}
}

// Value is the exact sum (what a strict operation sees).
func (c Counter) Value() int {
	v := c.settled
	for _, e := range c.recent {
		v += e.n
	}
	return v
}

// Range is the least and most a read that started at s may show: every increment acknowledged
// more than bound before s, and at most all of them.
func (c Counter) Range(s int64, bound time.Duration) (lo, hi int) {
	t := before(s, bound)
	lo = c.settled
	for _, e := range c.recent {
		if e.ret < t {
			lo += e.n
		}
	}
	return lo, c.Value()
}

// Prune folds increments that every read from floor on must see into the settled sum.
func (c Counter) Prune(floor int64, bound time.Duration) Counter {
	t := before(floor, bound)
	n := 0
	for _, e := range c.recent {
		if e.ret < t {
			n++
		}
	}
	if n == 0 {
		return c
	}
	out := Counter{settled: c.settled, recent: make([]cinc, 0, len(c.recent)-n)}
	for _, e := range c.recent {
		if e.ret < t {
			out.settled += e.n
		} else {
			out.recent = append(out.recent, e)
		}
	}
	return out
}

// Equal compares sums and recent increments.
func (c Counter) Equal(o Counter) bool {
	return c.settled == o.settled && slices.Equal(c.recent, o.recent)
}

// Hash agrees with Equal.
func (c Counter) Hash() uint64 {
	var h maphash.Hash
	h.SetSeed(seed)
	maphash.WriteComparable(&h, c.settled)
	for _, e := range c.recent {
		maphash.WriteComparable(&h, e)
	}
	return h.Sum64()
}
