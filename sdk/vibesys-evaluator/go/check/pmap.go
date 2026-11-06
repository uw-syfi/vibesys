package check

import (
	"sort"
)

// Value is what a PMap holds: comparable and hashable (Register, Window and Counter are).
type Value[V any] interface {
	Equal(V) bool
	Hash() uint64
}

// PMap is a persistent string-keyed map for model states: Set returns a new map and shares the
// rest. The search keeps one state per explored (linearized set, state) pair, so a state that
// copies all its items on every step (every slot of a resource) costs memory and time linear in its
// size per step; PMap keeps a shared immutable base and a small overlay, merged when it grows.
// Its hash is order-independent and maintained incrementally, so Hash is O(1).
type PMap[V Value[V]] struct {
	base    *pbase[V]
	overlay []pentry[V] // sorted by key; overrides base
	hash    uint64
}

type pbase[V any] struct {
	m    map[string]V
	hash uint64
}

type pentry[V any] struct {
	k string
	v V
}

const overlayMax = 16

func entryHash(k string, h uint64) uint64 {
	var x uint64 = 14695981039346656037
	for i := 0; i < len(k); i++ {
		x ^= uint64(k[i])
		x *= 1099511628211
	}
	return mix64(x ^ mix64(h))
}

// Get returns the value at k.
func (p PMap[V]) Get(k string) (V, bool) {
	i := sort.Search(len(p.overlay), func(i int) bool { return p.overlay[i].k >= k })
	if i < len(p.overlay) && p.overlay[i].k == k {
		return p.overlay[i].v, true
	}
	if p.base != nil {
		v, ok := p.base.m[k]
		return v, ok
	}
	var zero V
	return zero, false
}

// Set returns the map with k set to v.
func (p PMap[V]) Set(k string, v V) PMap[V] {
	old, had := p.Get(k)
	h := p.hash
	if had {
		h -= entryHash(k, old.Hash())
	}
	h += entryHash(k, v.Hash())
	i := sort.Search(len(p.overlay), func(i int) bool { return p.overlay[i].k >= k })
	var ov []pentry[V]
	if i < len(p.overlay) && p.overlay[i].k == k {
		ov = make([]pentry[V], len(p.overlay))
		copy(ov, p.overlay)
		ov[i].v = v
	} else {
		ov = make([]pentry[V], 0, len(p.overlay)+1)
		ov = append(ov, p.overlay[:i]...)
		ov = append(ov, pentry[V]{k, v})
		ov = append(ov, p.overlay[i:]...)
	}
	n := PMap[V]{base: p.base, overlay: ov, hash: h}
	if len(ov) > overlayMax {
		n = n.compact()
	}
	return n
}

func (p PMap[V]) compact() PMap[V] {
	size := len(p.overlay)
	if p.base != nil {
		size += len(p.base.m)
	}
	m := make(map[string]V, size)
	if p.base != nil {
		for k, v := range p.base.m {
			m[k] = v
		}
	}
	for _, e := range p.overlay {
		m[e.k] = e.v
	}
	return PMap[V]{base: &pbase[V]{m: m, hash: p.hash}, hash: p.hash}
}

// Len is the number of keys.
func (p PMap[V]) Len() int {
	n := len(p.overlay)
	if p.base != nil {
		n += len(p.base.m)
		for _, e := range p.overlay {
			if _, ok := p.base.m[e.k]; ok {
				n--
			}
		}
	}
	return n
}

// Range calls f for every key in order.
func (p PMap[V]) Range(f func(k string, v V)) {
	keys := make([]string, 0, p.Len())
	seen := map[string]bool{}
	for _, e := range p.overlay {
		keys = append(keys, e.k)
		seen[e.k] = true
	}
	if p.base != nil {
		for k := range p.base.m {
			if !seen[k] {
				keys = append(keys, k)
			}
		}
	}
	sort.Strings(keys)
	for _, k := range keys {
		v, _ := p.Get(k)
		f(k, v)
	}
}

// MapRecent applies f to the values set since the last compaction (the overlay), e.g. to prune
// them; values in the shared base were pruned when they were last set. O(overlay).
func (p PMap[V]) MapRecent(f func(V) V) PMap[V] {
	var ov []pentry[V]
	h := p.hash
	for i, e := range p.overlay {
		w := f(e.v)
		if w.Equal(e.v) {
			continue
		}
		if ov == nil {
			ov = make([]pentry[V], len(p.overlay))
			copy(ov, p.overlay)
		}
		h += entryHash(e.k, w.Hash()) - entryHash(e.k, e.v.Hash())
		ov[i].v = w
	}
	if ov == nil {
		return p
	}
	return PMap[V]{base: p.base, overlay: ov, hash: h}
}

// Hash agrees with Equal.
func (p PMap[V]) Hash() uint64 { return p.hash }

// Equal compares contents.
func (p PMap[V]) Equal(o PMap[V]) bool {
	if p.hash != o.hash {
		return false
	}
	if p.base == o.base {
		if len(p.overlay) == len(o.overlay) {
			same := true
			for i := range p.overlay {
				if p.overlay[i].k != o.overlay[i].k || !p.overlay[i].v.Equal(o.overlay[i].v) {
					same = false
					break
				}
			}
			if same {
				return true
			}
		}
	}
	if p.Len() != o.Len() {
		return false
	}
	eq := true
	p.Range(func(k string, v V) {
		if !eq {
			return
		}
		w, ok := o.Get(k)
		eq = ok && v.Equal(w)
	})
	return eq
}
