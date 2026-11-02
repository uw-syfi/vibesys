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
	stores map[app.Scope]*memstore.Store
}

var _ app.Stores = (*Stores)(nil)

// New returns Stores whose stores are opened with the rules.
func New(rules store.Rules) *Stores {
	return &Stores{rules: rules, stores: map[app.Scope]*memstore.Store{}}
}

// Find implements app.Stores.
func (m *Stores) Find(ctx context.Context, scope app.Scope) (store.Store, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := scope.Validate(); err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.stores[scope]
	if !ok {
		return nil, store.Mark(&core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("task %s has no store in %s", scope.Task, scope.Repository)}, store.ErrNoStore)
	}
	return s, nil
}

// Create implements app.Stores.
func (m *Stores) Create(ctx context.Context, scope app.Scope) (store.Store, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := scope.Validate(); err != nil {
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

// Close implements app.Stores. Memory has nothing to give up.
func (m *Stores) Close() error { return nil }

// Known implements app.Stores.
func (m *Stores) Known(ctx context.Context) ([]app.Scope, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]app.Scope, 0, len(m.stores))
	for scope := range m.stores {
		out = append(out, scope)
	}
	slices.SortFunc(out, func(a, b app.Scope) int {
		if c := strings.Compare(a.Repository, b.Repository); c != 0 {
			return c
		}
		return strings.Compare(a.Task, b.Task)
	})
	return out, nil
}
