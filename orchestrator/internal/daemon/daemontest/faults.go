package daemontest

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
)

// Bounds is a daemon.Bounds a test ends itself. A bound lasts until Expire, which ends every bound
// handed out and not yet released: it stands for the time a hanging call would have taken.
type Bounds struct {
	mu      sync.Mutex
	cancels []context.CancelFunc
	// Issued counts the bounds handed out.
	Issued int
}

// Bound implements daemon.Bounds.
func (b *Bounds) Bound(ctx context.Context) (context.Context, context.CancelFunc) {
	c, cancel := context.WithCancel(ctx)
	b.mu.Lock()
	defer b.mu.Unlock()
	b.Issued++
	b.cancels = append(b.cancels, cancel)
	return c, cancel
}

// Expire ends every bound handed out so far.
func (b *Bounds) Expire() {
	b.mu.Lock()
	cancels := b.cancels
	b.cancels = nil
	b.mu.Unlock()
	for _, cancel := range cancels {
		cancel()
	}
}

// Crash is what a FaultStore panics with at a crash point: the daemon's process ends there, with
// whatever it had done in the world done and whatever it had not, not. The caller recovers it,
// reopens the store and starts a new daemon.
type Crash struct{ At string }

// The points a FaultStore can crash at, inside one step of the effect worker.
const (
	CrashAfterClaim     = "after claim"
	CrashBeforeComplete = "before complete"
)

// ErrInjected is the error a FaultStore returns for a call it was told to fail.
var ErrInjected = errors.New("daemontest: injected store failure")

// FaultStore wraps a store with the failures a disk has: settling calls that fail, and a process
// that ends between the calls of one step. Every fault is a count or a one-shot, so that a test
// settles once they are spent.
type FaultStore struct {
	store.Store
	mu sync.Mutex
	// FailComplete and FailRelease fail that many calls before any succeeds.
	FailComplete, FailRelease int
	// FailMessages fails that many reads of a run's messages before any succeeds, and every one when it
	// is negative.
	FailMessages int
	// FailMessage is the same for reads of one message by its id.
	FailMessage int
	// CrashAt, when set to a crash point, ends the process the next time the daemon reaches it.
	CrashAt string
	// Committed lists every effect committed through this store, with the sequence of its result.
	Committed []core.Effect
}

// Commit implements store.Store and records the effects.
func (f *FaultStore) Commit(r store.Result) (store.Committed, error) {
	c, err := f.Store.Commit(r)
	if err == nil {
		f.mu.Lock()
		f.Committed = append(f.Committed, r.Effects...)
		f.mu.Unlock()
	}
	return c, err
}

func (f *FaultStore) crash(at string) {
	f.mu.Lock()
	hit := f.CrashAt == at
	if hit {
		f.CrashAt = ""
	}
	f.mu.Unlock()
	if hit {
		panic(Crash{At: at})
	}
}

// Claim implements store.Store.
func (f *FaultStore) Claim(now time.Time, limit int) ([]store.Claimed, error) {
	claimed, err := f.Store.Claim(now, limit)
	if err == nil && len(claimed) > 0 {
		f.crash(CrashAfterClaim)
	}
	return claimed, err
}

// Complete implements store.Store.
func (f *FaultStore) Complete(ids ...store.OutboxID) error {
	f.crash(CrashBeforeComplete)
	f.mu.Lock()
	fail := f.FailComplete > 0
	if fail {
		f.FailComplete--
	}
	f.mu.Unlock()
	if fail {
		return ErrInjected
	}
	return f.Store.Complete(ids...)
}

// Messages implements store.Store.
func (f *FaultStore) Messages(run core.RunID, after uint64, limit int) ([]store.MessageRecord, error) {
	f.mu.Lock()
	fail := f.FailMessages != 0
	if f.FailMessages > 0 {
		f.FailMessages--
	}
	f.mu.Unlock()
	if fail {
		return nil, ErrInjected
	}
	return f.Store.Messages(run, after, limit)
}

// Message implements store.Store.
func (f *FaultStore) Message(id core.MessageID) (core.Message, bool, error) {
	f.mu.Lock()
	fail := f.FailMessage != 0
	if f.FailMessage > 0 {
		f.FailMessage--
	}
	f.mu.Unlock()
	if fail {
		return core.Message{}, false, ErrInjected
	}
	return f.Store.Message(id)
}

// Release implements store.Store.
func (f *FaultStore) Release(ids ...store.OutboxID) error {
	f.mu.Lock()
	fail := f.FailRelease > 0
	if fail {
		f.FailRelease--
	}
	f.mu.Unlock()
	if fail {
		return ErrInjected
	}
	return f.Store.Release(ids...)
}
