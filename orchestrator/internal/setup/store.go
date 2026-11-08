package setup

import (
	"reflect"
	"sync"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// ReadKind is what a read found.
type ReadKind uint8

// The kinds of read.
const (
	// ReadFacts: the task was read.
	ReadFacts ReadKind = iota + 1
	// ReadUnreadable: the task is there but cannot be read, such as a half-written manifest.
	ReadUnreadable
	// ReadFailure: the read itself failed.
	ReadFailure
)

// Read is the result of one read, numbered by the order the reads were started in.
type Read struct {
	Seq    uint64
	Task   string
	Kind   ReadKind
	Facts  Facts
	Detail string
}

// Observation is the latest a run's reads have shown. A failed read keeps the last facts and sets
// Failure; the next good read clears it.
type Observation struct {
	Seq        uint64
	Task       string
	Readable   bool
	Facts      Facts
	Unreadable string
	Failure    string
	Revision   uint64
}

// Derived is the observation's checks: Derive over its facts, or Unreadable, or all Pending while
// nothing has been read yet.
func (o Observation) Derived(p Policy) Result {
	switch {
	case o.Readable:
		return Derive(o.Facts, p)
	case o.Unreadable != "":
		return Unreadable(o.Unreadable)
	default:
		return Unreadable("reading the task")
	}
}

// Store holds the latest observation of each run, in memory. Its mutex is its own; nothing here
// calls out while holding it.
type Store struct {
	mu   sync.Mutex
	runs map[core.RunID]Observation
	seq  uint64
}

// NewStore returns an empty store.
func NewStore() *Store { return &Store{runs: map[core.RunID]Observation{}} }

// Next numbers a read about to start: every number is higher than the one before, whoever starts the
// read, so the later of two reads is the one a Put keeps.
func (s *Store) Next() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.seq++
	return s.seq
}

// Put records a read and reports whether the observation changed. A read numbered below the held one
// is ignored, as is one that leaves the observation's content as it is: neither moves the revision.
// The facts are held, not copied; callers treat them as immutable.
func (s *Store) Put(run core.RunID, r Read) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	held, had := s.runs[run]
	if had && r.Seq < held.Seq {
		return false
	}
	var next Observation
	switch r.Kind {
	case ReadFacts:
		next = Observation{Task: r.Task, Readable: true, Facts: r.Facts}
	case ReadUnreadable:
		next = Observation{Task: r.Task, Unreadable: r.Detail}
	case ReadFailure:
		next = held
		if held.Task != r.Task {
			next = Observation{Task: r.Task}
		}
		next.Failure = r.Detail
		if next.Failure == "" {
			next.Failure = "read failed"
		}
	default:
		return false
	}
	next.Seq, next.Revision = r.Seq, held.Revision
	if had && sameContent(held, next) {
		held.Seq = next.Seq
		s.runs[run] = held
		return false
	}
	next.Revision++
	s.runs[run] = next
	return true
}

func sameContent(a, b Observation) bool {
	a.Seq, b.Seq = 0, 0
	return reflect.DeepEqual(a, b)
}

// Get returns the run's observation.
func (s *Store) Get(run core.RunID) (Observation, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	o, ok := s.runs[run]
	return o, ok
}

// Revision is the run's observation revision; zero means none.
func (s *Store) Revision(run core.RunID) uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.runs[run].Revision
}

// Forget drops a run's observation.
func (s *Store) Forget(run core.RunID) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.runs, run)
}
