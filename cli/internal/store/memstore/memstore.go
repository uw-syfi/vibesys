// Package memstore is an in-memory store.Store for tests of the code that uses a Store.
//
// It is not a stub: it runs the same ledger.Engine as internal/store/filestore, so it joins,
// reuses, numbers and rejects exactly what the on-disk store does. It differs only in that its
// changes go to memory and cannot fail, and that nothing outlives it. It passes the same
// contract suite, internal/store/storetest.
package memstore

import (
	"fmt"
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/ledger"
)

// Store is an in-memory store.Store, safe for concurrent use. Create it with New.
type Store struct {
	*ledger.Engine
}

// Option configures New.
type Option func(*config)

type config struct{ rules store.Rules }

// WithRules sets the decisions of internal/core that depend on the task. Without them every saved
// result with a submission's key answers it, and a submission that leaves Samples at zero joins
// nothing.
func WithRules(rules store.Rules) Option {
	return func(c *config) { c.rules = rules }
}

// New returns an empty Store.
func New(opts ...Option) *Store {
	var c config
	for _, opt := range opts {
		opt(&c)
	}
	engine, err := ledger.NewEngine(newMemory(), c.rules)
	if err != nil {
		panic(fmt.Sprintf("memstore: an empty store did not start: %v", err))
	}
	return &Store{Engine: engine}
}

// memory keeps the bodies of events and logs, which the ledger only counts.
type memory struct {
	events map[core.RunID][]core.Event
	logs   map[core.JobID][]byte
}

func newMemory() *memory {
	return &memory{events: map[core.RunID][]core.Event{}, logs: map[core.JobID][]byte{}}
}

func (m *memory) Commit(change ledger.Change) error {
	switch c := change.(type) {
	case ledger.AddEvent:
		m.events[c.Event.Run] = append(m.events[c.Event.Run], c.Event)
	case ledger.AddLog:
		m.logs[c.Job] = append(m.logs[c.Job], c.Data...)
	}
	return nil
}

// Recover is the empty state: memory does not outlive its Store, and Commit never fails, so the
// Engine asks only when it starts.
func (m *memory) Recover() (ledger.Snapshot, error) { return ledger.Snapshot{}, nil }

func (m *memory) Events(run core.RunID, first, last uint64) ([]core.Event, error) {
	return slices.Clone(m.events[run][first-1 : last]), nil
}

func (m *memory) Log(job core.JobID, from, to uint64) ([]byte, error) {
	return m.logs[job][from:to], nil
}
