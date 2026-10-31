package server

import (
	"context"
	"sync"
	"sync/atomic"
	"time"
)

// silenceGuard cancels a call that goes quiet. It counts the time since the
// last frame, not the time since the call began, so a stream of any length
// lives as long as each gap between its frames is short. The call's own cap
// stays the bound on a stream that never ends.
type silenceGuard struct {
	beats chan struct{}
	quit  chan struct{}
	done  chan struct{}
	once  sync.Once
	// ranOut is the window that elapsed without a frame, zero while none has.
	ranOut atomic.Int64
}

// guardSilence starts the guard: cancel runs when first passes without a
// frame, then when between passes without the next one. A zero between ends
// the guard at the first frame (the stream's first event is the only bound), and
// two zero windows start nothing.
func guardSilence(cancel context.CancelFunc, first, between time.Duration) *silenceGuard {
	g := &silenceGuard{beats: make(chan struct{}, 1), quit: make(chan struct{}), done: make(chan struct{})}
	if first <= 0 && between <= 0 {
		close(g.done)
		return g
	}
	if first <= 0 {
		first = between
	}
	go g.watch(cancel, first, between)
	return g
}

func (g *silenceGuard) watch(cancel context.CancelFunc, first, between time.Duration) {
	defer close(g.done)
	window := first
	timer := time.NewTimer(window)
	defer timer.Stop()
	for {
		select {
		case <-g.beats:
			if between <= 0 {
				return
			}
			window = between
			timer.Reset(window)
		case <-timer.C:
			select {
			case <-g.beats:
				// A frame landed as the window ran out: the stream was alive.
				if between <= 0 {
					return
				}
				window = between
				timer.Reset(window)
				continue
			default:
			}
			g.ranOut.Store(int64(window))
			cancel()
			return
		case <-g.quit:
			return
		}
	}
}

// frame records that a frame reached the client, which restarts the window.
func (g *silenceGuard) frame() {
	select {
	case g.beats <- struct{}{}:
	default:
	}
}

// silentFor is the window the stream stayed silent through, or zero when it did not.
func (g *silenceGuard) silentFor() time.Duration { return time.Duration(g.ranOut.Load()) }

// stop ends the guard and waits for it, so no timer outlives the call.
func (g *silenceGuard) stop() {
	g.once.Do(func() { close(g.quit) })
	<-g.done
}
