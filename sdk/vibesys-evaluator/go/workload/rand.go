package workload

import (
	"fmt"
	"math"
	"sort"
	"sync"
)

const (
	golden    = 0x9E3779B97F4A7C15
	fnvOffset = 0xCBF29CE484222325
	fnvPrime  = 0x100000001B3
	unit      = 1.0 / (1 << 53)
)

// Rand is a deterministic random stream named by a seed and labels, bit-identical to the Python
// SDK's Rand: NewRand(seed, labels...) here and Rand(seed, *labels) there draw the same values
// with the same methods, on every platform (sdk/vibesys-evaluator/fixtures/rand.json checks it).
// The algorithms are part of the versioned contract: SplitMix64 from a state that FNV-1a folds
// the labels into. A Rand is not safe for concurrent use.
type Rand struct {
	key   foldKey
	state uint64
}

type foldKey struct {
	state      uint64
	pending    uint64
	hasPending bool
}

// NewRand starts the stream of seed and labels. A label is a string or a non-negative integer
// (any integer type); NewRand panics on any other label.
func NewRand(seed uint64, labels ...any) *Rand {
	return newRand(foldKey{state: seed}.fold(labels))
}

func newRand(key foldKey) *Rand {
	return &Rand{key: key, state: key.start()}
}

// Child returns the stream NewRand(seed, parentLabels..., labels...), whatever this one has drawn.
func (r *Rand) Child(labels ...any) *Rand {
	return newRand(r.key.fold(labels))
}

func finalize(value uint64) uint64 {
	value = (value ^ (value >> 30)) * 0xBF58476D1CE4E5B9
	value = (value ^ (value >> 27)) * 0x94D049BB133111EB
	return value ^ (value >> 31)
}

func mix(random, stream uint64) uint64 { return finalize(random + golden*(stream+1)) }

func fnv1a(label string) uint64 {
	value := uint64(fnvOffset)
	for i := 0; i < len(label); i++ {
		value = (value ^ uint64(label[i])) * fnvPrime
	}
	return value
}

func (k foldKey) fold(labels []any) foldKey {
	for _, label := range labels {
		if text, ok := label.(string); ok {
			if k.hasPending {
				k.state = finalize(k.pending)
			}
			k.pending, k.hasPending = k.state^fnv1a(text), true
			continue
		}
		n := integerLabel(label)
		base := k.state
		if k.hasPending {
			base = k.pending
		}
		k.state, k.hasPending = mix(base, n), false
	}
	return k
}

func integerLabel(label any) uint64 {
	var signed int64
	switch v := label.(type) {
	case int:
		signed = int64(v)
	case int8:
		signed = int64(v)
	case int16:
		signed = int64(v)
	case int32:
		signed = int64(v)
	case int64:
		signed = v
	case uint:
		return uint64(v)
	case uint8:
		return uint64(v)
	case uint16:
		return uint64(v)
	case uint32:
		return uint64(v)
	case uint64:
		return v
	default:
		panic(fmt.Sprintf("workload.Rand labels are strings or integers, got %T", label))
	}
	if signed < 0 {
		panic(fmt.Sprintf("workload.Rand integer labels are >= 0, got %d", signed))
	}
	return uint64(signed)
}

func (k foldKey) start() uint64 {
	if k.hasPending {
		return finalize(k.pending)
	}
	return k.state
}

func (r *Rand) next() uint64 {
	r.state += golden
	return finalize(r.state)
}

// Uint64 returns one raw draw (Python: below(2**64)).
func (r *Rand) Uint64() uint64 { return r.next() }

// Random returns a float64 uniform in [0, 1), from 53 bits of one draw.
func (r *Rand) Random() float64 { return float64(r.next()>>11) * unit }

// Below returns an integer in [0, n): one draw modulo n. It panics when n < 1.
func (r *Rand) Below(n int) int {
	if n < 1 {
		panic(fmt.Sprintf("workload.Rand.Below needs n >= 1, got %d", n))
	}
	return int(r.next() % uint64(n))
}

// Weighted returns an index drawn with probability proportional to its weight; never one of
// weight 0. Integer weights draw Below(sum) and walk the weights; other weights draw
// Random() * sum (an exactly rounded sum, as Python's math.fsum). It panics on an empty list, a
// negative or non-finite weight, or a sum that is not positive.
func (r *Rand) Weighted(weights []float64) int {
	integers := len(weights) > 0
	for _, w := range weights {
		if w < 0 || math.IsInf(w, 0) || math.IsNaN(w) {
			panic(fmt.Sprintf("workload.Rand.Weighted needs finite non-negative weights, got %v", weights))
		}
		if w != math.Trunc(w) {
			integers = false
		}
	}
	if len(weights) == 0 {
		panic("workload.Rand.Weighted needs at least one weight")
	}
	if integers {
		var total uint64
		for _, w := range weights {
			total += uint64(w)
		}
		if total < 1 {
			panic("workload.Rand.Weighted needs a positive sum of weights")
		}
		draw := r.next() % total
		for i, w := range weights {
			if draw < uint64(w) {
				return i
			}
			draw -= uint64(w)
		}
		return len(weights) - 1
	}
	total := fsum(weights)
	if !(total > 0) {
		panic("workload.Rand.Weighted needs a positive sum of weights")
	}
	point := float64(r.Random() * total)
	last := 0
	for i, w := range weights {
		if w > 0 {
			last = i
		}
	}
	for i, w := range weights {
		if point < w {
			return i
		}
		point -= w
	}
	return last
}

var sqrt3 = math.Sqrt(3)

// Normal returns an approximately standard normal value: Irwin-Hall of four uniforms, within
// 2 sqrt(3).
func (r *Rand) Normal() float64 {
	a := r.Random()
	a = a + r.Random()
	a = a + r.Random()
	a = a + r.Random()
	return float64(a-2.0) * sqrt3
}

// Zipf returns a rank in [0, n) with weight (rank + 1)^-s, by the inverse CDF of one uniform
// draw; 0 is the most likely. It panics when n < 1 or s is negative or not finite.
func (r *Rand) Zipf(n int, s float64) int {
	if n < 1 || !(s >= 0) || math.IsInf(s, 0) {
		panic(fmt.Sprintf("workload.Rand.Zipf needs n >= 1 and a finite s >= 0, got n=%d, s=%v", n, s))
	}
	cumulative := zipfCumulative(n, s)
	point := float64(r.Random() * cumulative[n-1])
	return min(sort.SearchFloat64s(cumulative, point), n-1)
}

type zipfKey struct {
	n int
	s float64
}

var zipfCache = struct {
	sync.Mutex
	tables map[zipfKey][]float64
}{tables: map[zipfKey][]float64{}}

const zipfCacheLimit = 64

func zipfCumulative(n int, s float64) []float64 {
	key := zipfKey{n, s}
	zipfCache.Lock()
	table, ok := zipfCache.tables[key]
	zipfCache.Unlock()
	if ok {
		return table
	}
	weights := make([]float64, n)
	total := 0.0
	for k := range n {
		weights[k] = zipfWeight(k+1, s)
		total += weights[k]
	}
	running := 0.0
	for k, w := range weights {
		running += w / total
		weights[k] = running
	}
	zipfCache.Lock()
	if len(zipfCache.tables) >= zipfCacheLimit {
		clear(zipfCache.tables)
	}
	zipfCache.tables[key] = weights
	zipfCache.Unlock()
	return weights
}

func zipfWeight(rank int, s float64) float64 {
	k := float64(rank)
	switch s {
	case 1:
		return 1 / k
	case 0.5:
		return 1 / math.Sqrt(k)
	case 0.75:
		return 1 / float64(math.Sqrt(k)*math.Sqrt(math.Sqrt(k)))
	}
	return fdlibmExp(float64(-s * fdlibmLog(k)))
}
