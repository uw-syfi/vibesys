package check

import (
	"fmt"
	"math"
	"runtime"
	"runtime/debug"
	"runtime/metrics"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/anishathalye/porcupine"
)

// pollEvery is how many model steps a search takes between looks at its budgets: often enough
// that a search cannot outgrow the memory budget by much between looks, rarely enough to cost
// nothing measurable.
const pollEvery = 256

// Abort reasons of a search.
const (
	running int32 = iota
	abortMemory
	abortTime
	abortSteps
)

// runner holds the budgets shared by every search of a check.
type runner struct {
	o        Options
	workers  int
	deadline time.Time // the total budget
	mu       sync.Mutex
	searches map[*control]bool
	peak     int64
	oldLimit int64
	setLimit bool
}

// control is one search's budget and abort switch.
type control struct {
	abort     atomic.Int32
	steps     atomic.Int64
	deadline  time.Time
	peakHeap  int64 // guarded by runner.mu
	abortHeap int64 // guarded by runner.mu
}

// newRunner starts the budgets of one check. runtimeHeap says the memory budget bounds the Go
// runtime's heap (Options.HeapBytes was nil).
func newRunner(o Options, began time.Time, runtimeHeap bool) *runner {
	w := o.Workers
	if w <= 0 {
		w = runtime.GOMAXPROCS(0)
	}
	r := &runner{o: o, workers: w, deadline: began.Add(o.TotalTimeout), searches: map[*control]bool{}}
	if runtimeHeap {
		// A soft limit makes the GC work harder near the budget; the step-driven watchdog is the
		// hard one.
		r.oldLimit, r.setLimit = debug.SetMemoryLimit(o.MemoryBytes), true
	}
	return r
}

func (r *runner) stop() {
	if r.setLimit {
		debug.SetMemoryLimit(r.oldLimit)
	}
}

func (r *runner) totalExpired() bool { return !time.Now().Before(r.deadline) }

func (r *runner) peakHeap() int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.peak
}

// collect returns an aborted search's memory before the next one starts.
func (r *runner) collect() {
	if r.setLimit {
		runtime.GC()
	}
}

// begin registers a search with its own time budget.
func (r *runner) begin(timeout time.Duration) *control {
	deadline := time.Now().Add(timeout)
	if deadline.After(r.deadline) {
		deadline = r.deadline
	}
	c := &control{deadline: deadline}
	r.mu.Lock()
	r.searches[c] = true
	r.mu.Unlock()
	return c
}

func (r *runner) end(c *control) {
	r.mu.Lock()
	delete(r.searches, c)
	r.mu.Unlock()
}

// step counts one model step and, every pollEvery, looks at the budgets: the search's own steps
// and time, then the heap, which, when over the budget, aborts the search that has taken the most
// steps (unless one is already unwinding). It says whether the search may go on.
func (r *runner) step(c *control) bool {
	n := c.steps.Add(1)
	if n%pollEvery == 0 {
		r.poll(c, n)
	}
	return c.abort.Load() == running
}

func (r *runner) poll(c *control, n int64) {
	if r.o.MaxSearchSteps > 0 && n >= r.o.MaxSearchSteps {
		c.abort.CompareAndSwap(running, abortSteps)
	}
	if !time.Now().Before(c.deadline) {
		c.abort.CompareAndSwap(running, abortTime)
	}
	heap := r.o.HeapBytes()
	r.mu.Lock()
	defer r.mu.Unlock()
	r.peak = max(r.peak, heap)
	for s := range r.searches {
		s.peakHeap = max(s.peakHeap, heap)
	}
	if heap <= r.o.MemoryBytes {
		return
	}
	var victim *control
	for s := range r.searches {
		if s.abort.Load() != running {
			return // one is unwinding: give its memory back first
		}
		if victim == nil || s.steps.Load() > victim.steps.Load() {
			victim = s
		}
	}
	if victim != nil {
		victim.abort.Store(abortMemory)
		victim.abortHeap = heap
	}
}

// heapObjects is the Go runtime's heap in use (live and not yet swept objects).
func heapObjects() int64 {
	s := []metrics.Sample{{Name: "/memory/classes/heap/objects:bytes"}}
	metrics.Read(s)
	return int64(s[0].Value.Uint64())
}

// pin is porcupine's input: one event or one fault of a partition.
type pin[I, O any] struct {
	op     Op[I, O]
	origin *Call
	fault  *Fault
}

// stateSet is the set of states the system may be in (several after a step that allows more
// than one outcome).
type stateSet[S any] struct {
	states []S
	hashes []uint64
	hash   uint64
}

func mix64(x uint64) uint64 {
	x ^= x >> 33
	x *= 0xff51afd7ed558ccd
	x ^= x >> 33
	x *= 0xc4ceb9fe1a85ec53
	x ^= x >> 33
	return x
}

// porcupineModel wraps a spec for porcupine: state sets, faults, and the budget (an aborted
// search sees every step fail, unwinds, and its result is discarded). c may be nil.
func porcupineModel[S, I, O any](sp *Spec[S, I, O], keys []string, r *runner, c *control) porcupine.Model {
	makeSet := func(states []S) *stateSet[S] {
		set := &stateSet[S]{}
		for _, s := range states {
			h := sp.Hash(s)
			dup := false
			for k, other := range set.states {
				if set.hashes[k] == h && sp.Equal(other, s) {
					dup = true
					break
				}
			}
			if !dup {
				set.states = append(set.states, s)
				set.hashes = append(set.hashes, h)
				set.hash += mix64(h)
			}
		}
		return set
	}
	return porcupine.Model{
		Init: func() any { return makeSet([]S{sp.Init(keys)}) },
		Step: func(state, input, _ any) (bool, any) {
			if c != nil && !r.step(c) {
				return false, nil
			}
			set := state.(*stateSet[S])
			in := input.(*pin[I, O])
			var next []S
			for _, s := range set.states {
				if in.fault != nil {
					if sp.Fault == nil {
						next = append(next, s)
					} else {
						next = append(next, sp.Fault(s, *in.fault)...)
					}
					continue
				}
				states, _ := sp.Step(s, in.op)
				next = append(next, states...)
			}
			if len(next) == 0 {
				return false, nil
			}
			return true, makeSet(next)
		},
		Equal: func(a, b any) bool {
			x, y := a.(*stateSet[S]), b.(*stateSet[S])
			if x.hash != y.hash || len(x.states) != len(y.states) {
				return false
			}
			for i, s := range x.states {
				found := false
				for j, t := range y.states {
					if x.hashes[i] == y.hashes[j] && sp.Equal(s, t) {
						found = true
						break
					}
				}
				if !found {
					return false
				}
			}
			return true
		},
		Hash: func(state any) uint64 { return state.(*stateSet[S]).hash },
		DescribeOperation: func(input, _ any) string {
			in := input.(*pin[I, O])
			if in.fault != nil {
				return fmt.Sprintf("FAULT %s %s", in.fault.Kind, in.fault.Target)
			}
			return describeOp(sp, in.op)
		},
		DescribeState: func(state any) string { return describeSet(sp, state.(*stateSet[S])) },
	}
}

func describeOp[S, I, O any](sp *Spec[S, I, O], op Op[I, O]) string {
	if sp.DescribeOp != nil {
		return sp.DescribeOp(op)
	}
	if op.Unknown {
		return fmt.Sprintf("%v -> ?", op.In)
	}
	return fmt.Sprintf("%v -> %v", op.In, op.Out)
}

func describeSet[S, I, O any](sp *Spec[S, I, O], set *stateSet[S]) string {
	var parts []string
	for _, s := range set.states {
		if sp.DescribeState != nil {
			parts = append(parts, sp.DescribeState(s))
		} else {
			parts = append(parts, fmt.Sprintf("%v", s))
		}
	}
	if len(parts) == 1 {
		return parts[0]
	}
	return "one of {" + strings.Join(parts, " | ") + "}"
}

// buildOps turns a partition into porcupine operations: its events (an unknown outcome returns
// at the end of its effect window or of the history), then the faults, each with its Floor and a
// display lane. It fills the partition report's sizes.
func buildOps[S, I, O any](sp *Spec[S, I, O], p *partition[I, O], faults []Fault, horizon int64, pr *PartitionReport) []porcupine.Operation {
	type span struct{ call, ret int64 }
	n := len(p.events) + len(faults)
	spans := make([]span, 0, n)
	clients := true
	for _, e := range p.events {
		ret := e.ret
		if e.unknown {
			ret = horizon
			if sp.EffectWindow > 0 {
				ret = min(horizon, max(e.ret, e.call+int64(sp.EffectWindow)))
			}
			pr.Unknowns++
		}
		if e.write {
			pr.Writes++
		} else {
			pr.Reads++
		}
		if e.origin == nil || e.origin.Client < 0 {
			clients = false
		}
		spans = append(spans, span{e.call, ret})
	}
	for _, f := range faults {
		spans = append(spans, span{f.Start, f.End})
	}
	pr.Ops = len(p.events)
	at := func(i int) (int64, int64) { return spans[i].call, spans[i].ret }
	floors := computeFloors(n, at)
	pr.MaxInFlight = maxInFlight(len(p.events), at)
	var lanes []int
	if clients {
		lanes = make([]int, n)
		top := 0
		for i, e := range p.events {
			lanes[i] = e.origin.Client
			top = max(top, lanes[i]+1)
		}
		for j := range faults {
			lanes[len(p.events)+j] = top
		}
	} else {
		lanes = assignLanes(n, at)
	}
	ops := make([]porcupine.Operation, 0, n)
	for i, e := range p.events {
		in := &pin[I, O]{origin: e.origin, op: Op[I, O]{Keys: e.keys, In: e.in, Out: e.out, Unknown: e.unknown, Write: e.write, Call: spans[i].call, Return: spans[i].ret, Floor: floors[i]}}
		ops = append(ops, porcupine.Operation{ClientId: lanes[i], Input: in, Call: spans[i].call, Return: spans[i].ret})
	}
	for j := range faults {
		i := len(p.events) + j
		f := faults[j]
		ops = append(ops, porcupine.Operation{ClientId: lanes[i], Input: &pin[I, O]{fault: &f}, Call: f.Start, Return: f.End})
	}
	return ops
}

// computeFloors: floor(x) = min{call(y) : ret(y) >= call(x)}, the earliest call of any op that
// may be linearized after x (an op linearized after x cannot have returned before x was called).
func computeFloors(n int, at func(int) (int64, int64)) []int64 {
	idx := make([]int, n)
	for i := range idx {
		idx[i] = i
	}
	sort.Slice(idx, func(a, b int) bool { _, ra := at(idx[a]); _, rb := at(idx[b]); return ra < rb })
	suffix := make([]int64, n+1)
	suffix[n] = math.MaxInt64
	for k := n - 1; k >= 0; k-- {
		c, _ := at(idx[k])
		suffix[k] = min(suffix[k+1], c)
	}
	floors := make([]int64, n)
	for i := range n {
		c, _ := at(i)
		k := sort.Search(n, func(k int) bool { _, r := at(idx[k]); return r >= c })
		floors[i] = suffix[k]
	}
	return floors
}

// maxInFlight is the most of the first n ops in flight at one time.
func maxInFlight(n int, at func(int) (int64, int64)) int {
	type edge struct {
		t     int64
		delta int
	}
	edges := make([]edge, 0, 2*n)
	for i := range n {
		c, r := at(i)
		edges = append(edges, edge{c, 1}, edge{r, -1})
	}
	sort.Slice(edges, func(a, b int) bool {
		if edges[a].t != edges[b].t {
			return edges[a].t < edges[b].t
		}
		return edges[a].delta > edges[b].delta // closed intervals: calls before returns at a tie
	})
	cur, best := 0, 0
	for _, e := range edges {
		cur += e.delta
		best = max(best, cur)
	}
	return best
}

// assignLanes gives each op a display lane so that ops of one lane never overlap.
func assignLanes(n int, at func(int) (int64, int64)) []int {
	idx := make([]int, n)
	for i := range idx {
		idx[i] = i
	}
	sort.SliceStable(idx, func(a, b int) bool { ca, _ := at(idx[a]); cb, _ := at(idx[b]); return ca < cb })
	var laneEnd []int64
	lanes := make([]int, n)
	for _, i := range idx {
		c, r := at(i)
		placed := false
		for l, e := range laneEnd {
			if e < c {
				lanes[i], laneEnd[l], placed = l, r, true
				break
			}
		}
		if !placed {
			lanes[i] = len(laneEnd)
			laneEnd = append(laneEnd, r)
		}
	}
	return lanes
}

// Prefix doubling: the first prefix checked is about prefixStart responses long; a cut with more
// than maxCutWrites writes in flight is not taken (each is an unknown outcome there, and their
// combinations multiply the search's states).
const (
	prefixStart  = 100
	maxCutWrites = 8
)

// prefixCuts picks, for each doubling length k, the response time among responses [k, 2k) with
// the fewest writes in flight.
func prefixCuts[I, O any](events []*event[I, O]) []int64 {
	var rets, wcalls, wrets []int64
	for _, e := range events {
		if !e.unknown {
			rets = append(rets, e.ret)
		}
		if e.write {
			wcalls = append(wcalls, e.call)
			r := e.ret
			if e.unknown {
				r = math.MaxInt64
			}
			wrets = append(wrets, r)
		}
	}
	sort.Slice(rets, func(i, j int) bool { return rets[i] < rets[j] })
	sort.Slice(wcalls, func(i, j int) bool { return wcalls[i] < wcalls[j] })
	sort.Slice(wrets, func(i, j int) bool { return wrets[i] < wrets[j] })
	inFlight := func(t int64) int {
		called := sort.Search(len(wcalls), func(i int) bool { return wcalls[i] > t })
		done := sort.Search(len(wrets), func(i int) bool { return wrets[i] > t })
		return called - done
	}
	var cuts []int64
	limit := len(rets) * 3 / 4
	for k := prefixStart; k < limit; k *= 2 {
		best, bestN := -1, maxCutWrites+1
		for i := k; i < min(2*k, limit); i++ {
			if n := inFlight(rets[i]); n < bestN {
				best, bestN = i, n
				if n == 0 {
					break
				}
			}
		}
		if best >= 0 {
			cuts = append(cuts, rets[best])
		}
	}
	return cuts
}

// prefix is the partition's history up to t: calls made by t, with responses after t unknown
// (they may or may not have taken effect), and the faults that started by t. culpritsUnknown
// also makes the responses at exactly t unknown.
func prefix[I, O any](p *partition[I, O], faults []Fault, t int64, culpritsUnknown bool) (*partition[I, O], []Fault) {
	q := &partition[I, O]{name: p.name, keys: p.keys}
	for _, e := range p.events {
		if e.call > t {
			continue
		}
		c := *e
		if c.ret > t || (culpritsUnknown && c.ret == t) {
			if !c.write {
				continue // a read still in flight tells nothing yet
			}
			c.unknown = true
		}
		q.events = append(q.events, &c)
	}
	var fs []Fault
	for _, f := range faults {
		if f.Start < t || (f.Start == t && !culpritsUnknown) {
			fs = append(fs, f)
		}
	}
	return q, fs
}

// searchPartition searches one partition under the budgets: prefixes of doubling length, then
// the whole.
func searchPartition[S, I, O any](sp *Spec[S, I, O], r *runner, p *partition[I, O], faults []Fault, horizon int64) PartitionReport {
	pr := PartitionReport{Name: p.name, Keys: len(p.keys), SampledOut: p.sampledOut}
	ops := buildOps(sp, p, faults, horizon, &pr)
	switch {
	case r.totalExpired():
		pr.Status, pr.Reason = StatusSkipped, "the check's total time budget ran out before this partition started"
		return pr
	case len(p.events) > r.o.MaxOpsPerPartition:
		pr.Status, pr.Reason = StatusTooLarge, fmt.Sprintf("%d ops after sampling > MaxOpsPerPartition %d (%d writes, which are never sampled)", len(p.events), r.o.MaxOpsPerPartition, pr.Writes)
		return pr
	}
	began := time.Now()
	c := r.begin(r.o.PartitionTimeout)
	model := porcupineModel(sp, p.keys, r, c)
	res := porcupine.Ok
	for _, t := range prefixCuts(p.events) {
		q, fs := prefix(p, faults, t, false)
		var scratch PartitionReport
		res = porcupine.CheckOperationsTimeout(model, buildOps(sp, q, fs, horizon, &scratch), 0)
		if res != porcupine.Ok {
			if res == porcupine.Illegal && c.abort.Load() == running {
				pr.Reason = fmt.Sprintf("found in the prefix of the first %d of %d ops", len(q.events), len(p.events))
			}
			break
		}
	}
	if res == porcupine.Ok {
		res = porcupine.CheckOperationsTimeout(model, ops, 0)
	}
	r.end(c)
	pr.Duration = time.Since(began)
	pr.Steps = c.steps.Load()
	r.mu.Lock()
	pr.PeakHeapBytes, pr.abortHeap = c.peakHeap, c.abortHeap
	r.mu.Unlock()
	switch c.abort.Load() {
	case abortMemory:
		pr.Status = StatusMemory
		pr.Reason = fmt.Sprintf("heap reached %s > budget %s after %v and %d search steps; aborted", mb(pr.abortHeap), mb(r.o.MemoryBytes), pr.Duration.Round(time.Millisecond), pr.Steps)
		r.collect()
	case abortTime:
		pr.Status = StatusTimeout
		pr.Reason = fmt.Sprintf("no verdict within %v (%d search steps)", pr.Duration.Round(time.Millisecond), pr.Steps)
	case abortSteps:
		pr.Status = StatusTimeout
		pr.Reason = fmt.Sprintf("no verdict within MaxSearchSteps %d", r.o.MaxSearchSteps)
	default:
		if res == porcupine.Illegal {
			pr.Status = StatusIllegal
		} else {
			pr.Status, pr.Reason = StatusOK, ""
		}
	}
	return pr
}

func mb(b int64) string { return fmt.Sprintf("%.0f MB", float64(b)/1e6) }
