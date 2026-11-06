package production

import (
	"context"
	"errors"
	"time"
)

const (
	// handOverEvery is how often a starting host looks at the state directory's lock again.
	handOverEvery = 20 * time.Millisecond
	// handOverLooks bounds the wait to 5 s of looks, inside the 10 s a client gives a start. It guards
	// only against a host that stopped answering and never exits.
	handOverLooks = 250
)

// outgoingHostOf says whether the holder of a state directory may be a host on its way out, for a
// caller that holds the lock of root. Nothing else can hold the state of a root while its own lock is
// held by someone else, and a holder that does not say its root (one writing its record now, or of an
// older release) cannot be told apart, so it gets the same bounded wait. A holder that names another
// root is a live host sharing this state directory: waiting for it would only delay the refusal.
func (e *HeldError) outgoingHostOf(root string) bool {
	return e.Holder.GetRoot() == "" || e.Holder.GetRoot() == root
}

// AwaitHandOver runs open, which takes the state directory's lock, until the lock is no longer held
// by an outgoing host of root. The caller must hold root's own lock. A process releases its locks as
// it exits, in no order this program controls, so a new host can win the root's lock while the old
// one still holds the state's: that is a hand-over in progress, not a refusal. It returns what open
// last returned, which is the refusal when ctx ends or the bound passes first. after is how it waits
// between looks; what ends the wait is always the lock.
func AwaitHandOver[T any](ctx context.Context, root string, after func(time.Duration) <-chan time.Time, open func() (T, error)) (T, error) {
	for looks := 0; ; looks++ {
		value, err := open()
		var held *HeldError
		if !errors.As(err, &held) || !held.outgoingHostOf(root) || looks == handOverLooks {
			return value, err
		}
		select {
		case <-ctx.Done():
			return value, err
		case <-after(handOverEvery):
		}
	}
}
