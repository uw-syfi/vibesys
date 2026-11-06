package check

import (
	"bytes"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/anishathalye/porcupine"
)

// explain finds the first violation of a failing partition: the earliest response time T such
// that the history up to T is not linearizable, where calls still in flight at T count as outcome
// unknown (linearizability is prefix-closed, so this is monotone in T and a binary search finds
// it). The responses at T are the culprits: the history before them is linearizable, one of its
// linearizations gives a state the system could have been in, and Step says why each culprit
// cannot follow it. porcupine's own partial linearization can point past the first violation;
// this cannot.
func explain[S, I, O any](sp *Spec[S, I, O], r *runner, p *partition[I, O], faults []Fault, horizon int64) Failure {
	f := Failure{Partition: p.name}
	c := r.begin(r.o.ExplainTimeout, false)
	defer r.end(c)
	search := func(q *partition[I, O], fs []Fault, verbose bool) (porcupine.CheckResult, porcupine.LinearizationInfo, []porcupine.Operation, porcupine.Model) {
		var scratch PartitionReport
		ops := buildOps(sp, q, fs, horizon, &scratch)
		model := porcupineModel(sp, q.keys, r, c)
		var res porcupine.CheckResult
		var info porcupine.LinearizationInfo
		if verbose {
			res, info = porcupine.CheckOperationsVerbose(model, ops, 0)
		} else {
			res = porcupine.CheckOperationsTimeout(model, ops, 0)
		}
		if c.abort.Load() != running {
			res = porcupine.Unknown
		}
		return res, info, ops, model
	}
	upTo := func(t int64) porcupine.CheckResult {
		q, fs := prefix(p, faults, t, false)
		res, _, _, _ := search(q, fs, false)
		return res
	}

	var times []int64
	start := int64(0)
	for i, e := range p.events {
		if !e.unknown {
			times = append(times, e.ret)
		}
		if i == 0 || e.call < start {
			start = e.call
		}
	}
	for _, x := range faults {
		times = append(times, x.Start)
	}
	sort.Slice(times, func(i, j int) bool { return times[i] < times[j] })
	times = dedup(times)
	if len(times) == 0 {
		f.Explanation = "no op of this partition has a known outcome"
		return f
	}
	lo, hi := 0, len(times)-1 // invariant: the prefix up to times[hi] is illegal
	if res := upTo(times[hi]); res != porcupine.Illegal {
		f.Explanation = fmt.Sprintf("the search confirmed a violation, but the search for the first one got %s within ExplainTimeout %v", res, r.o.ExplainTimeout)
		return f
	}
	for lo < hi {
		mid := (lo + hi) / 2
		switch upTo(times[mid]) {
		case porcupine.Illegal:
			hi = mid
		case porcupine.Ok:
			lo = mid + 1
		default:
			f.Explanation = fmt.Sprintf("the search confirmed a violation; the search for the first one ran out of ExplainTimeout %v between t=+%s and t=+%s", r.o.ExplainTimeout, rel(times[lo], start), rel(times[hi], start))
			return f
		}
	}
	t := times[hi]

	var culprits []*event[I, O]
	for _, e := range p.events {
		if !e.unknown && e.ret == t {
			culprits = append(culprits, e)
		}
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "first violation at t=+%s (of %s of history in this partition):\n", rel(t, start), rel(times[len(times)-1], start))
	if len(culprits) == 0 {
		fmt.Fprintf(&sb, "  the fault at t=+%s leaves no state the model accepts\n", rel(t, start))
	}
	for _, e := range culprits {
		fmt.Fprintf(&sb, "  response of %s\n", describeEvent(sp, e, start))
	}

	// The state just before: linearize the history up to t without the culprits (calls still in
	// flight stay unknown) and replay that order to its end; Step says why each culprit cannot
	// come next. (A culprit may fit earlier in another order; the search says no order fits all.)
	q, fs := prefix(p, faults, t, false)
	kept := q.events[:0:0]
	for _, e := range q.events {
		culprit := false
		for _, x := range culprits {
			if e.origin == x.origin && e.call == x.call && e.ret == x.ret && fmt.Sprint(e.in) == fmt.Sprint(x.in) {
				culprit = true
			}
		}
		if !culprit {
			kept = append(kept, e)
		}
	}
	q.events = kept
	if res, info, ops, model := search(q, fs, true); res == porcupine.Ok {
		if lins := info.PartialLinearizations(); len(lins) > 0 && len(lins[0]) > 0 {
			state := model.Init()
			for _, idx := range lins[0][0] {
				_, state = model.Step(state, ops[idx].Input, nil)
			}
			set := state.(*stateSet[S])
			fmt.Fprintf(&sb, "without it, the history up to then is linearizable; a state it can end in: %s\n", describeSet(sp, set))
			for _, x := range culprits {
				op := Op[I, O]{Keys: x.keys, In: x.in, Out: x.out, Write: x.write, Call: x.call, Return: x.ret}
				for k, s := range set.states {
					if k >= 3 {
						break
					}
					if next, why := sp.Step(s, op); len(next) == 0 && why != "" {
						fmt.Fprintf(&sb, "  model: %s\n", why)
					}
				}
			}
		}
	}

	// Context: the partition's other ops from 1 s before the first culprit until its response.
	if len(culprits) > 0 {
		x := culprits[0]
		byCall := append([]*event[I, O](nil), p.events...)
		sort.SliceStable(byCall, func(i, j int) bool { return byCall[i].call < byCall[j].call })
		n := 0
		for _, e := range byCall {
			if e == x || e.call > x.ret || (!e.unknown && e.ret < x.call-int64(time.Second)) {
				continue
			}
			if n == 0 {
				fmt.Fprintf(&sb, "other ops of this partition from 1 s before it until its response:\n")
			}
			if n++; n > 12 {
				fmt.Fprintf(&sb, "  ...\n")
				break
			}
			fmt.Fprintf(&sb, "  %s\n", describeEvent(sp, e, start))
		}
		for _, y := range faults {
			if y.Start <= x.ret && y.End >= x.call-int64(5*time.Second) {
				fmt.Fprintf(&sb, "  fault %s %s at t=+%s\n", y.Kind, y.Target, rel(y.Start, start))
			}
		}
	}

	// The timeline of the failing prefix.
	q2, fs2 := prefix(p, faults, t, false)
	if n := len(q2.events) + len(fs2); n > r.o.MaxTimelineOps {
		fmt.Fprintf(&sb, "(no timeline: the failing prefix has %d ops > MaxTimelineOps %d)\n", n, r.o.MaxTimelineOps)
	} else if res, info, _, model := search(q2, fs2, true); res == porcupine.Illegal {
		var notes []porcupine.Annotation
		for _, y := range fs2 {
			notes = append(notes, porcupine.Annotation{Tag: "faults", Start: y.Start, End: y.End, Description: y.Kind + " " + y.Target, BackgroundColor: "#ffb3b3"})
		}
		for _, x := range culprits {
			notes = append(notes, porcupine.Annotation{Tag: "first violation", Start: x.call, End: x.ret, Description: "a response no order explains", BackgroundColor: "#ff6666"})
		}
		info.AddAnnotations(notes)
		var buf bytes.Buffer
		if err := porcupine.Visualize(model, info, &buf); err == nil {
			f.Timeline = buf.Bytes()
		}
	}
	f.Explanation = sb.String()
	return f
}

func describeEvent[S, I, O any](sp *Spec[S, I, O], e *event[I, O], start int64) string {
	desc := describeOp(sp, Op[I, O]{Keys: e.keys, In: e.in, Out: e.out, Unknown: e.unknown, Write: e.write, Call: e.call, Return: e.ret})
	ret := "+" + rel(e.ret, start)
	if e.unknown {
		ret = "?"
	}
	from := ""
	if e.origin != nil {
		from = fmt.Sprintf(" (%s)", where(e.origin))
	}
	return fmt.Sprintf("[+%s, %s] %s%s", rel(e.call, start), ret, desc, from)
}

func rel(t, start int64) string {
	return time.Duration(t - start).Round(time.Microsecond).String()
}

func dedup(xs []int64) []int64 {
	out := xs[:0]
	for i, x := range xs {
		if i == 0 || x != xs[i-1] {
			out = append(out, x)
		}
	}
	return out
}
