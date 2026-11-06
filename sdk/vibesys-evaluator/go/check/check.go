package check

import (
	"fmt"
	"iter"
	"math"
	"math/rand"
	"sort"
	"strings"
	"sync"
	"time"
)

// Check checks a history against specs, all of which must hold, under the options' budgets.
//
// For each spec it projects the history phase by phase (stopping after a phase with violations),
// fails when the share of unknown outcomes is above MaxLostShare, splits the pieces into
// partitions (connected components of keys), samples reads per key, and searches the partitions,
// largest first and in parallel, each first over prefixes of doubling length (an early violation
// is found at a fraction of the cost) and then whole. A partition aborted for memory is retried
// alone once the others are done. The first violations of failing partitions are explained.
//
// The verdict is FAIL when some spec has a violation, an illegal partition or too many unknown
// outcomes; otherwise UNKNOWN when something could not be decided (a budget ran out, a task or
// capture bug); otherwise PASS.
func Check(h *History, specs []AnySpec, o Options) *Report {
	began := time.Now()
	runtimeHeap := o.HeapBytes == nil
	o = o.withDefaults()
	r := newRunner(o, began, runtimeHeap)
	defer r.stop()
	report := &Report{}
	for _, s := range specs {
		report.Specs = append(report.Specs, s.run(h, r))
	}
	report.PeakHeapBytes = r.peakHeap()
	report.Elapsed = time.Since(began)
	report.finish()
	return report
}

func (sp *Spec[S, I, O]) run(h *History, r *runner) *SpecReport {
	rep := &SpecReport{Name: sp.Name}
	o := r.o
	if missing := sp.missing(); missing != "" {
		rep.Errors = append(rep.Errors, "the spec has no "+missing)
		rep.finish(o)
		return rep
	}

	// Projection, phase by phase, in as many passes as Project makes.
	out := &Projection[I, O]{spec: sp.Name}
	var loadErr error
	stopped := false
	counted := 0 // phases whose calls are in the coverage
	phases := func(yield func([]Call) bool) {
		for i := range h.Phases {
			calls, err := h.Phase(i)
			if err != nil {
				loadErr = err
				return
			}
			out.origins = map[*Call]*Call{}
			if i == counted {
				counted++
				rep.Coverage.Calls += len(calls)
				for c := range calls {
					if calls[c].Lost() {
						rep.Coverage.LostCalls++
					}
				}
			}
			if !yield(calls) {
				return
			}
			if len(out.violations) > 0 {
				stopped = i+1 < h.Phases
				return
			}
		}
	}
	out.origins = map[*Call]*Call{}
	err := sp.Project(iter.Seq[[]Call](phases), out)
	switch {
	case loadErr != nil:
		rep.Errors = append(rep.Errors, fmt.Sprintf("reading the history: %v", loadErr))
	case err != nil:
		rep.Errors = append(rep.Errors, fmt.Sprintf("projection: %v", err))
	}
	rep.Errors = append(rep.Errors, out.errors...)
	rep.Violations = out.violations
	if stopped {
		rep.Warnings = append(rep.Warnings, "projection stopped after the first phase with violations")
	}
	rep.Coverage.Pieces = len(out.pieces) + out.keyless
	rep.Coverage.UnknownPieces = out.unknown
	rep.Coverage.Keyless = out.keyless
	rep.Coverage.Faults = len(h.Faults)
	if len(rep.Errors) > 0 || len(rep.Violations) > 0 {
		rep.finish(o)
		return rep
	}
	if n := rep.Coverage.Pieces; n > 0 && o.MaxLostShare >= 0 && float64(out.unknown) > o.MaxLostShare*float64(n) {
		rep.LostShareExceeded = true
		rep.finish(o)
		return rep
	}

	// Unknown reads tell nothing; faults must be quiescent.
	events := out.pieces[:0]
	for _, e := range out.pieces {
		if e.unknown && !e.write {
			rep.Coverage.UnknownReadsDropped++
			continue
		}
		events = append(events, e)
	}
	out.pieces = nil
	if errs := quiescence(events, h.Faults); len(errs) > 0 {
		rep.Errors = append(rep.Errors, errs...)
		rep.finish(o)
		return rep
	}
	horizon := int64(math.MinInt64)
	for i := range events {
		horizon = max(horizon, events[i].ret)
	}
	for _, f := range h.Faults {
		horizon = max(horizon, f.End)
	}
	horizon++ // an unknown outcome's latest effect, when the spec has no EffectWindow
	faults := h.Faults
	if sp.Fault == nil {
		faults = nil
	}

	parts := split(events)
	rng := rand.New(rand.NewSource(o.Seed))
	for _, p := range parts {
		rep.Coverage.Keys += len(p.keys)
		p.sample(o.MaxReadsPerKey, rng)
		rep.Coverage.ReadsSampledOut += p.sampledOut
	}
	sort.SliceStable(parts, func(i, j int) bool { return len(parts[i].events) > len(parts[j].events) })
	rep.HeapBeforeSearch = o.HeapBytes()

	// Search: largest first, in parallel; memory aborts retried alone.
	reports := make([]PartitionReport, len(parts))
	jobs := make(chan int)
	var wg sync.WaitGroup
	for range r.workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range jobs {
				reports[i] = searchPartition(sp, r, parts[i], faults, horizon)
			}
		}()
	}
	for i := range parts {
		jobs <- i
	}
	close(jobs)
	wg.Wait()
	for i := range parts {
		if reports[i].Status != StatusMemory || r.totalExpired() {
			continue
		}
		first := strings.SplitN(reports[i].Reason, ";", 2)[0]
		r.collect()
		again := searchPartition(sp, r, parts[i], faults, horizon)
		again.Duration += reports[i].Duration
		again.Reason = strings.TrimPrefix(again.Reason+"; ", "; ") + "retried alone after a memory abort next to other partitions (" + first + ")"
		reports[i] = again
	}
	rep.Partitions = reports

	// Explanations, smallest failing partitions first (they are the cheapest).
	var failing []int
	for i := range parts {
		if reports[i].Status == StatusIllegal {
			failing = append(failing, i)
		}
	}
	sort.SliceStable(failing, func(a, b int) bool { return len(parts[failing[a]].events) < len(parts[failing[b]].events) })
	for n, i := range failing {
		if n >= o.MaxExplained {
			rep.Failures = append(rep.Failures, Failure{Partition: parts[i].name, Explanation: fmt.Sprintf("(not explained: only the first %d failing partitions are)", o.MaxExplained)})
			continue
		}
		rep.Failures = append(rep.Failures, explain(sp, r, parts[i], faults, horizon))
	}
	rep.finish(o)
	return rep
}

func (sp *Spec[S, I, O]) missing() string {
	var missing []string
	if sp.Name == "" {
		missing = append(missing, "Name")
	}
	if sp.Project == nil {
		missing = append(missing, "Project")
	}
	if sp.Init == nil {
		missing = append(missing, "Init")
	}
	if sp.Step == nil {
		missing = append(missing, "Step")
	}
	if sp.Equal == nil {
		missing = append(missing, "Equal")
	}
	if sp.Hash == nil {
		missing = append(missing, "Hash")
	}
	return strings.Join(missing, ", ")
}

// quiescence reports faults that some piece was in flight across.
func quiescence[I, O any](events []event[I, O], faults []Fault) []string {
	var errs []string
	for _, f := range faults {
		if f.End < f.Start {
			errs = append(errs, fmt.Sprintf("fault %s %s ends before it starts", f.Kind, f.Target))
			continue
		}
		for i := range events {
			if e := &events[i]; e.call <= f.End && f.Start <= e.ret && !e.unknown {
				errs = append(errs, fmt.Sprintf("fault %s %s after phase %d is not quiescent: %s was in flight", f.Kind, f.Target, f.AfterPhase, where(e.origin)))
				break
			}
		}
	}
	return errs
}

// partition is a connected component of keys and its events.
type partition[I, O any] struct {
	name       string
	keys       []string
	events     []*event[I, O]
	sampledOut int
}

// split groups events into connected components of keys (union-find over each piece's keys).
func split[I, O any](events []event[I, O]) []*partition[I, O] {
	parent := map[string]string{}
	var find func(string) string
	find = func(k string) string {
		p, ok := parent[k]
		if !ok {
			parent[k] = k
			return k
		}
		if p == k {
			return k
		}
		root := find(p)
		parent[k] = root
		return root
	}
	for i := range events {
		keys := events[i].keys
		a := find(keys[0])
		for _, k := range keys[1:] {
			if b := find(k); b != a {
				parent[b] = a
			}
		}
	}
	byRoot := map[string]*partition[I, O]{}
	var order []string
	for i := range events {
		e := &events[i]
		root := find(e.keys[0])
		p := byRoot[root]
		if p == nil {
			p = &partition[I, O]{}
			byRoot[root] = p
			order = append(order, root)
		}
		p.events = append(p.events, e)
	}
	keysOf := map[string][]string{}
	for k := range parent {
		root := find(k)
		keysOf[root] = append(keysOf[root], k)
	}
	parts := make([]*partition[I, O], 0, len(order))
	for _, root := range order {
		p := byRoot[root]
		p.keys = keysOf[root]
		sort.Strings(p.keys)
		p.name = strings.Join(p.keys, ",")
		if len(p.keys) > 3 {
			p.name = fmt.Sprintf("%s,... (%d keys)", strings.Join(p.keys[:3], ","), len(p.keys))
		}
		parts = append(parts, p)
	}
	return parts
}

// sample keeps at most limit reads per key (a seeded uniform sample); writes stay.
func (p *partition[I, O]) sample(limit int, rng *rand.Rand) {
	if limit < 0 {
		return
	}
	reads := map[string][]int{}
	for i, e := range p.events {
		if !e.write {
			reads[e.keys[0]] = append(reads[e.keys[0]], i)
		}
	}
	keys := make([]string, 0, len(reads))
	for k := range reads {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	drop := map[int]bool{}
	for _, k := range keys {
		idx := reads[k]
		if len(idx) <= limit {
			continue
		}
		rng.Shuffle(len(idx), func(a, b int) { idx[a], idx[b] = idx[b], idx[a] })
		for _, i := range idx[limit:] {
			drop[i] = true
		}
	}
	if len(drop) == 0 {
		return
	}
	kept := make([]*event[I, O], 0, len(p.events)-len(drop))
	for i, e := range p.events {
		if !drop[i] {
			kept = append(kept, e)
		}
	}
	p.sampledOut = len(drop)
	p.events = kept
}
