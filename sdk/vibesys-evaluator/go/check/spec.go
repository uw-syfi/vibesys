package check

import (
	"fmt"
	"iter"
	"time"
)

// Spec is one view of a history that must hold: a projection of its calls onto keyed pieces, and
// a model of one key's state that each piece steps. The check passes when, for every partition
// (the keys that atomic pieces join), some order of the partition's pieces that respects real
// time is one the model accepts. A task may give several Specs over one history; all must hold.
//
// S is the model's state, I a piece's input and O its output. Step must treat S as immutable and
// return new states; Equal and Hash define state identity for the search's cache (Hash must
// agree with Equal), so states that differ only in how they were reached should compare equal.
type Spec[S, I, O any] struct {
	// Name names the spec in reports.
	Name string
	// Project turns the history into pieces. It ranges over phases, which yields each phase's
	// calls in order, and adds pieces and violations to out. A phase's calls are released when
	// the next is read: keep facts, not calls. phases can be ranged more than once, and each
	// pass reads the history again, phase by phase: a piece that needs a fact about the whole
	// history (every key a listing could have returned, an index of names, the ids lost creates
	// may have made) is added in a second pass over facts the first pass collected, in the
	// memory of one phase and the facts. An error is a task bug: the check's verdict is UNKNOWN.
	Project func(phases iter.Seq[[]Call], out *Projection[I, O]) error
	// Init is the state of a partition over keys (sorted; one key unless atomic pieces joined
	// several).
	Init func(keys []string) S
	// Step returns the states the system may be in after op (several when the model allows more
	// than one outcome, as for an op whose outcome is unknown), or none and the reason the op
	// cannot follow s.
	Step func(s S, op Op[I, O]) ([]S, string)
	// Fault returns the states of the partition over keys (as Init's) after a fault. nil means
	// faults change no state: they are then not copied into partitions.
	Fault func(s S, keys []string, f Fault) []S
	Equal func(a, b S) bool
	Hash  func(s S) uint64
	// EffectWindow bounds when an op whose outcome is unknown takes effect: within this long
	// after its call, or never. 0 means at any time until the end of the history, which keeps
	// every unknown outcome open for the rest of its partition and makes the search much harder.
	EffectWindow time.Duration
	// DescribeState and DescribeOp render states and ops in failure explanations and timelines
	// (optional: %v otherwise).
	DescribeState func(s S) string
	DescribeOp    func(op Op[I, O]) string
}

// Piece is one model operation projected from a call onto the keys it touches.
type Piece[I, O any] struct {
	// Keys are the state keys the piece touches. Usually one: an operation over several keys is
	// projected into one piece per key. Several keys mean the piece is atomic over them, and the
	// check then searches the keys' connected component as one partition. No keys means the
	// piece touches no state (a lost login, say): it is counted, an unknown outcome toward
	// MaxLostShare too, and never searched.
	Keys []string
	In   I
	Out  O
	// Unknown means the outcome is unknown (a lost response, a 5xx, an error that may have been
	// applied): Out is ignored and Step must allow both outcomes. An unknown read tells nothing
	// and is dropped.
	Unknown bool
	// Write marks a piece that may change state. Reads may be sampled (a dropped read can hide a
	// violation, never invent one); writes are always kept.
	Write bool
	// Origin is the call the piece comes from, for reports. The check keeps a copy without the
	// response bodies.
	Origin *Call
	// Start and End override Origin's times (a multi-step call whose piece is one step); zero
	// means Origin's.
	Start, End int64
}

// Op is a piece as Step sees it.
type Op[I, O any] struct {
	Keys    []string
	In      I
	Out     O
	Unknown bool
	Write   bool
	// Call and Return are the op's real-time window. An unknown outcome's Return is where its
	// effect window ends (Spec.EffectWindow), or the end of the history.
	Call, Return int64
	// Floor is a time every op the search may still place after this one was called at or after:
	// a model may forget time-based detail no such op can observe (versions that are settled for
	// every read from Floor on), which keeps its states small and comparable.
	Floor int64
}

// Violation is a response that is wrong on its own, found without the model's state (a malformed
// body, a status the operation never answers): the check fails without a search.
type Violation struct {
	Spec    string
	Call    *Call
	Message string
}

// Projection collects one spec's pieces and violations as Project adds them.
type Projection[I, O any] struct {
	pieces     []event[I, O]
	violations []Violation
	errors     []string
	unknown    int
	keyless    int
	lostCalls  map[[2]uint64]bool // calls (phase, index) with an unknown piece
	lostNoCall int                // unknown pieces with no Origin: one call each
	origins    map[*Call]*Call
	spec       string
}

// Add adds a piece.
func (p *Projection[I, O]) Add(piece Piece[I, O]) {
	start, end := piece.Start, piece.End
	if piece.Origin != nil {
		if start == 0 {
			start = piece.Origin.Start
		}
		if end == 0 {
			end = piece.Origin.End
		}
	}
	switch {
	case piece.Origin == nil && start == 0:
		p.error("a piece has neither an Origin nor its own times")
		return
	case end < start:
		p.error("a piece ends before it starts (%s)", where(piece.Origin))
		return
	}
	if piece.Unknown {
		p.unknown++
		switch o := piece.Origin; {
		case o == nil:
			p.lostNoCall++
		default:
			if p.lostCalls == nil {
				p.lostCalls = map[[2]uint64]bool{}
			}
			p.lostCalls[[2]uint64{uint64(o.Phase), o.Index}] = true
		}
	}
	if len(piece.Keys) == 0 {
		p.keyless++ // touches no state: only counted
		return
	}
	p.pieces = append(p.pieces, event[I, O]{
		keys: piece.Keys, in: piece.In, out: piece.Out, unknown: piece.Unknown, write: piece.Write,
		call: start, ret: end, origin: p.origin(piece.Origin),
	})
}

// Violation records a response that is wrong on its own.
func (p *Projection[I, O]) Violation(c *Call, format string, args ...any) {
	p.violations = append(p.violations, Violation{Spec: p.spec, Call: p.origin(c), Message: fmt.Sprintf(format, args...)})
}

func (p *Projection[I, O]) error(format string, args ...any) {
	if len(p.errors) < 20 {
		p.errors = append(p.errors, fmt.Sprintf(format, args...))
	}
}

// origin is c without its response bodies, shared by the pieces of one call.
func (p *Projection[I, O]) origin(c *Call) *Call {
	if c == nil {
		return nil
	}
	if o, ok := p.origins[c]; ok {
		return o
	}
	o := *c
	o.Steps = make([]Step, len(c.Steps))
	for i, s := range c.Steps {
		s.Body = nil
		o.Steps[i] = s
	}
	p.origins[c] = &o
	return &o
}

func where(c *Call) string {
	if c == nil {
		return "no origin"
	}
	return fmt.Sprintf("%s, phase %d op %d", c.Op, c.Phase, c.Index)
}

// AnySpec is a Spec of any state, input and output types: what Check and Main take.
type AnySpec interface {
	run(h *History, r *runner) *SpecReport
}

// event is a piece as the engine holds it.
type event[I, O any] struct {
	keys      []string
	in        I
	out       O
	unknown   bool
	write     bool
	call, ret int64
	origin    *Call
}
