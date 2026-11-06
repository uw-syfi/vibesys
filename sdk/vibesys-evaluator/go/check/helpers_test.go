package check_test

import (
	"fmt"
	"math"
	"math/rand"
	"slices"
	"sort"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check"
)

type write struct {
	v         int
	call, ret int64
	lp        int64 // linearization point, in [call, ret]
}

// randomWrites are overlapping writes of distinct values, some with an unknown outcome
// (ret = MaxInt64), in an order consistent with real time (by linearization point).
func randomWrites(rng *rand.Rand, n int) []write {
	var ws []write
	for i := range n {
		call := rng.Int63n(5000)
		ret := call + 1 + rng.Int63n(1500)
		lp := call + rng.Int63n(ret-call)
		if rng.Intn(8) == 0 {
			ret = math.MaxInt64
		}
		ws = append(ws, write{v: i + 1, call: call, ret: ret, lp: lp})
	}
	sort.Slice(ws, func(a, b int) bool { return ws[a].lp < ws[b].lp })
	return ws
}

// Window relaxes Register soundly: a read may show every value the linearized Register allows
// (so a valid history stays valid), and pruning at a floor changes nothing a read starting at or
// after it may show.
func TestWindowAllowsWhatRegisterAllowsAndPruningIsInvisible(t *testing.T) {
	const b = 1000 * time.Nanosecond
	for seed := range int64(300) {
		rng := rand.New(rand.NewSource(seed))
		reg, win := check.NewRegister(0), check.NewWindow(0)
		for _, x := range randomWrites(rng, 1+rng.Intn(12)) {
			reg, win = reg.Write(x.v, x.ret), win.Write(x.v, x.call, x.ret)
		}
		floor := rng.Int63n(8000)
		preg, pwin := reg.Prune(floor, b), win.Prune(floor, b)
		for range 20 {
			c := floor + rng.Int63n(4000)
			for _, v := range reg.Allowed(c, b) {
				if !win.MayShow(v, c, b) {
					t.Fatalf("seed %d: the Register allows %d at %d, the Window does not (%v)", seed, v, c, win.Allowed(c, b))
				}
			}
			if !slices.Equal(reg.Allowed(c, b), preg.Allowed(c, b)) || !sameSet(win.Allowed(c, b), pwin.Allowed(c, b)) {
				t.Fatalf("seed %d: pruning at %d changed what a read at %d may show", seed, floor, c)
			}
		}
		if preg.Len() > reg.Len() || pwin.Len() > win.Len() {
			t.Fatalf("pruning grew a state")
		}
	}
}

// A Window's state does not depend on the order of its writes, so its Equal and Hash merge the
// states that different interleavings reach.
func TestWindowAndCounterAreOrderInsensitive(t *testing.T) {
	for seed := range int64(100) {
		rng := rand.New(rand.NewSource(seed))
		ws := randomWrites(rng, 1+rng.Intn(8))
		build := func(order []write) (check.Window[int], check.Counter) {
			win, cnt := check.NewWindow(0), check.Counter{}
			for _, x := range order {
				win, cnt = win.Write(x.v, x.call, x.ret), cnt.Add(x.v, x.call, x.ret)
			}
			return win, cnt
		}
		w1, c1 := build(ws)
		shuffled := slices.Clone(ws)
		rng.Shuffle(len(shuffled), func(a, b int) { shuffled[a], shuffled[b] = shuffled[b], shuffled[a] })
		w2, c2 := build(shuffled)
		if !w1.Equal(w2) || w1.Hash() != w2.Hash() || !c1.Equal(c2) || c1.Hash() != c2.Hash() {
			t.Fatalf("seed %d: the order of writes changed a state", seed)
		}
	}
}

// A Counter's value is the sum of its increments; a read that started at s may show from the
// increments acknowledged more than bound before s up to all of them; pruning keeps both.
func TestCounterRangesAreTheBoundedStalenessRule(t *testing.T) {
	const b = 1000 * time.Nanosecond
	for seed := range int64(200) {
		rng := rand.New(rand.NewSource(seed))
		var cnt check.Counter
		ws := randomWrites(rng, rng.Intn(10))
		sum := 0
		for _, x := range ws {
			cnt = cnt.Add(x.v, x.call, x.ret)
			sum += x.v
		}
		if cnt.Value() != sum {
			t.Fatalf("value %d, want %d", cnt.Value(), sum)
		}
		floor := rng.Int63n(8000)
		pruned := cnt.Prune(floor, b)
		for range 10 {
			s := floor + rng.Int63n(4000)
			settled := 0
			for _, x := range ws {
				if x.ret < s-int64(b) {
					settled += x.v
				}
			}
			lo, hi := cnt.Range(s, b)
			plo, phi := pruned.Range(s, b)
			if lo != settled || hi != sum || plo != lo || phi != hi {
				t.Fatalf("seed %d at %d: range [%d,%d], pruned [%d,%d], want [%d,%d]", seed, s, lo, hi, plo, phi, settled, sum)
			}
		}
	}
}

// A PMap is a map: what was set is got, whatever the order and however often it compacts, and
// maps with equal contents are Equal with equal hashes.
func TestPMapIsAPersistentMap(t *testing.T) {
	for seed := range int64(100) {
		rng := rand.New(rand.NewSource(seed))
		var p, q check.PMap[check.Counter]
		want := map[string]int{}
		var keys []string
		for i := range rng.Intn(80) {
			k := fmt.Sprintf("k%d", rng.Intn(30))
			c := check.Counter{}.Add(i, 0, 0)
			before := p
			p = p.Set(k, c)
			if old, had := before.Get(k); (had && old.Value() != want[k]) || (!had && before.Len() != len(want)) {
				t.Fatalf("Set changed the map it was called on")
			}
			want[k] = i
			keys = append(keys, k)
		}
		if p.Len() != len(want) {
			t.Fatalf("len %d, want %d", p.Len(), len(want))
		}
		for k, v := range want {
			if got, ok := p.Get(k); !ok || got.Value() != v {
				t.Fatalf("%s: %v %v, want %d", k, got.Value(), ok, v)
			}
		}
		rng.Shuffle(len(keys), func(a, b int) { keys[a], keys[b] = keys[b], keys[a] })
		for _, k := range keys {
			q = q.Set(k, check.Counter{}.Add(want[k], 0, 0))
		}
		if !p.Equal(q) || p.Hash() != q.Hash() {
			t.Fatalf("seed %d: equal contents set in another order are not Equal", seed)
		}
		if _, ok := p.Get("absent"); ok {
			t.Fatalf("an absent key is present")
		}
	}
}

func sameSet(a, b []int) bool {
	a, b = slices.Clone(a), slices.Clone(b)
	slices.Sort(a)
	slices.Sort(b)
	return slices.Equal(a, b)
}
