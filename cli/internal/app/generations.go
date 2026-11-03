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

// storeGenerations serializes readers capturing a lifetime with the loop
// retiring it. Tokens are immutable and compared only under this lock.
type storeGenerations struct {
	mu     sync.Mutex
	active map[lifecycle.StoreID]*effectGeneration
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
}

// generationContext keeps the original token across nested reads, including
// when it has been retired in the meantime. Recapturing would bless stale data.
func (a *App) generationContext(ctx context.Context, id lifecycle.StoreID) context.Context {
	if g, ok := ctx.Value(generationKey{}).(*effectGeneration); ok && g.store == id {
		return ctx
	}
	return context.WithValue(ctx, generationKey{}, a.generations.get(id))
}
