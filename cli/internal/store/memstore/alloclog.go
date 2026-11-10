package memstore

import (
	"context"
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// AllocLog is an in-memory store.AllocLog, safe for concurrent use. The zero value is an empty log.
type AllocLog struct {
	mu     sync.Mutex
	inputs []alloc.Input
}

var _ store.AllocLog = (*AllocLog)(nil)

// Append implements store.AllocLog.
func (l *AllocLog) Append(ctx context.Context, in alloc.Input) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.inputs = append(l.inputs, in)
	return nil
}

// Load implements store.AllocLog.
func (l *AllocLog) Load(ctx context.Context) ([]alloc.Input, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	return slices.Clone(l.inputs), nil
}
