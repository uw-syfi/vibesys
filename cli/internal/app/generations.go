package app

import (
	"context"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

// effectGeneration identifies one lifetime of a store. Reads and effects capture
// it before I/O; only the loop retires it when the store is forgotten.
type effectGeneration struct{ store lifecycle.StoreID }
type generationKey struct{}

// storeObservation stamps a read independently of running effects. Only a
// newer accepted successful observation supersedes it within a lifetime.
type storeObservation struct{ generation *effectGeneration }
type observationKey struct{}

// storeGenerations serializes readers capturing a lifetime with the loop
// retiring it. Tokens are immutable and compared only under this lock.
type storeGenerations struct {
	mu           sync.Mutex
	active       map[lifecycle.StoreID]*effectGeneration
	observations map[lifecycle.StoreID]*storeObservation
}

func (s *storeGenerations) get(id lifecycle.StoreID) *effectGeneration {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.active == nil {
		s.active = map[lifecycle.StoreID]*effectGeneration{}
	}
	if s.active[id] == nil {
		s.active[id] = &effectGeneration{store: id}
	}
	return s.active[id]
}

func (s *storeGenerations) current(g *effectGeneration) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.active[g.store] == g
}

func (s *storeGenerations) retire(id lifecycle.StoreID) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.active, id)
	delete(s.observations, id)
}

// generationContext keeps the original token across nested reads, including
// when it has been retired in the meantime. Recapturing would bless stale data.
func (a *App) generationContext(ctx context.Context, id lifecycle.StoreID) context.Context {
	if g, ok := ctx.Value(generationKey{}).(*effectGeneration); ok && g.store == id {
		return ctx
	}
	return context.WithValue(ctx, generationKey{}, a.generations.get(id))
}

// observe captures the current read epoch without changing the store lifetime.
func (s *storeGenerations) observe(g *effectGeneration) *storeObservation {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.observations == nil {
		s.observations = map[lifecycle.StoreID]*storeObservation{}
	}
	if s.active[g.store] != g {
		return &storeObservation{generation: g}
	}
	if s.observations[g.store] == nil {
		s.observations[g.store] = &storeObservation{generation: g}
	}
	return s.observations[g.store]
}

func (s *storeGenerations) observed(o *storeObservation) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.observations[o.generation.store] == o
}

// advance is called only by the loop, after validating both stamps.
func (s *storeGenerations) advance(g *effectGeneration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.observations == nil {
		s.observations = map[lifecycle.StoreID]*storeObservation{}
	}
	s.observations[g.store] = &storeObservation{generation: g}
}

func (a *App) observationContext(ctx context.Context, id lifecycle.StoreID) context.Context {
	ctx = a.generationContext(ctx, id)
	if o, ok := ctx.Value(observationKey{}).(*storeObservation); ok && o.generation.store == id {
		return ctx
	}
	g := ctx.Value(generationKey{}).(*effectGeneration)
	return context.WithValue(ctx, observationKey{}, a.generations.observe(g))
}
