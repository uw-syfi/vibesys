package check

import (
	"hash/maphash"
	"math"
	"time"
)

// Register is one item under bounded staleness, in linearization order: the values written to it,
// each with the time its write returned. A read that started at call may show the latest version
// whose write returned more than bound before call (the settled one) and every version after it
// in linearization order. A version written by an op whose outcome is unknown returns at
// math.MaxInt64: it may show from then on and never settles. With bound 0 a Register is a
// linearizable register whose reads also see writes still in flight.
//
// A Register remembers the order of the writes of the last bound, so states the search reaches by
// different orders of concurrent writes differ; for an item written often by concurrent clients,
// Window is the order-insensitive relaxation. Register is a value: Write and Prune return a new
// one and share nothing mutable.
type Register[V comparable] struct {
	vals []V
	rets []int64 // rets[0] is math.MinInt64 for the initial value
}

// NewRegister starts at the initial (seeded) value, settled since forever.
func NewRegister[V comparable](initial V) Register[V] {
	return Register[V]{vals: []V{initial}, rets: []int64{math.MinInt64}}
}

// Latest is the value of the last write in linearization order (what a strict read sees).
func (r Register[V]) Latest() V { return r.vals[len(r.vals)-1] }

// Write appends a version written by an op that returned at ret (math.MaxInt64 if unknown).
func (r Register[V]) Write(v V, ret int64) Register[V] {
	n := len(r.vals)
	vals := make([]V, n+1)
	rets := make([]int64, n+1)
	copy(vals, r.vals)
	copy(rets, r.rets)
	vals[n], rets[n] = v, ret
	return Register[V]{vals: vals, rets: rets}
}

// settled is the index of the last version that returned before t (a read that starts more than
// the bound after a write's acknowledgement must reflect it).
func (r Register[V]) settled(t int64) int {
	s := 0
	for i := len(r.rets) - 1; i >= 0; i-- {
		if r.rets[i] < t {
			s = i
			break
		}
	}
	return s
}

// before is t - bound without wrapping around.
func before(t int64, bound time.Duration) int64 {
	if t < math.MinInt64+int64(bound) {
		return math.MinInt64
	}
	return t - int64(bound)
}

// Allowed lists the versions a read that started at call may show under bound.
func (r Register[V]) Allowed(call int64, bound time.Duration) []V {
	return r.vals[r.settled(before(call, bound)):]
}

// MayShow says whether a read that started at call may show v.
func (r Register[V]) MayShow(v V, call int64, bound time.Duration) bool {
	for _, x := range r.Allowed(call, bound) {
		if x == v {
			return true
		}
	}
	return false
}

// Prune forgets versions that no read starting at or after floor can show.
func (r Register[V]) Prune(floor int64, bound time.Duration) Register[V] {
	s := r.settled(before(floor, bound))
	if s == 0 {
		return r
	}
	return Register[V]{vals: r.vals[s:], rets: r.rets[s:]}
}

// Len is the number of versions kept.
func (r Register[V]) Len() int { return len(r.vals) }

// Equal compares versions and their times.
func (r Register[V]) Equal(o Register[V]) bool {
	if len(r.vals) != len(o.vals) {
		return false
	}
	for i := range r.vals {
		if r.vals[i] != o.vals[i] || r.rets[i] != o.rets[i] {
			return false
		}
	}
	return true
}

var seed = maphash.MakeSeed()

// Hash agrees with Equal.
func (r Register[V]) Hash() uint64 {
	var h maphash.Hash
	h.SetSeed(seed)
	for i := range r.vals {
		maphash.WriteComparable(&h, r.vals[i])
		maphash.WriteComparable(&h, r.rets[i])
	}
	return h.Sum64()
}
