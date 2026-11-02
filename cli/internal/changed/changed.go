// Package changed is the one way a waiter learns that something it watches may have changed,
// without polling and without a lost wake-up.
package changed

import "sync"

// Signal wakes everyone who waits on it each time Notify is called. The zero value is ready.
//
// A waiter takes the channel from Wait before it looks at what it watches, then looks, then
// blocks on the channel. A change made between the look and the block has already closed that
// channel, so the waiter wakes and looks again. That order is the whole contract.
type Signal struct {
	mu sync.Mutex
	ch chan struct{}
}

// Wait returns a channel that is closed by the next Notify.
func (s *Signal) Wait() <-chan struct{} {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ch == nil {
		s.ch = make(chan struct{})
	}
	return s.ch
}

// Notify wakes every waiter that took its channel before this call.
func (s *Signal) Notify() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ch != nil {
		close(s.ch)
		s.ch = nil
	}
}
