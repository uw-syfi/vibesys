// Package memstores is test support: app.Stores over in-memory stores, standing for the disk.
package memstores

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/memstore"
)

// Stores is app.Stores over in-memory stores. It stands for the disk in tests: building a new
// App over the same Stores is a daemon that restarts.
type Stores struct {
	rules store.Rules

	mu     sync.Mutex
	stores map[core.Scope]*memstore.Store
}

var _ app.Stores = (*Stores)(nil)

// New returns Stores whose stores are opened with the rules.
func New(rules store.Rules) *Stores {
	return &Stores{rules: rules, stores: map[core.Scope]*memstore.Store{}}
}

// Find implements app.Stores.
func (m *Stores) Find(ctx context.Context, scope core.Scope) (store.Store, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.stores[scope]
	if !ok {
		return nil, store.Mark(&core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("task %s has no store in %s", string(scope.Task()), string(scope.Repository()))}, store.ErrNoStore)
	}
	return s, nil
}

// Create implements app.Stores.
func (m *Stores) Create(ctx context.Context, scope core.Scope) (store.Store, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.stores[scope]
	if !ok {
		s = memstore.New(memstore.WithRules(m.rules))
		m.stores[scope] = s
	}
	return s, nil
}

// Forget implements app.Stores.
func (m *Stores) Forget(_ context.Context, scope core.Scope) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.stores, scope)
	return nil
}

// GivenUp implements app.Stores. Memory has no owner record to remove, so it gives up nothing.
func (m *Stores) GivenUp(ctx context.Context, scope core.Scope) ([]core.JobID, error) {
	_, err := m.Find(ctx, scope)
	return nil, err
}

// Reclaim implements app.Stores. Nothing is given up, so nothing is taken back.
func (m *Stores) Reclaim(ctx context.Context, scope core.Scope, _ core.JobID) error {
	_, err := m.Find(ctx, scope)
	return err
}

// Close implements app.Stores. Memory has nothing to give up.
func (m *Stores) Close() error { return nil }

// Known implements app.Stores.
func (m *Stores) Known(ctx context.Context) ([]core.Scope, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]core.Scope, 0, len(m.stores))
	for scope := range m.stores {
		out = append(out, scope)
	}
	slices.SortFunc(out, func(a, b core.Scope) int {
		if c := strings.Compare(string(a.Repository()), string(b.Repository())); c != 0 {
			return c
		}
		return strings.Compare(string(a.Task()), string(b.Task()))
	})
	return out, nil
}
