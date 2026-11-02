package envtest

import (
	"context"
	"sync"
	"time"
)

// Timers is the clock.Timers an environment under test is given. A timer fires only when the
// test fires it, so no result depends on how long anything took. The test waits for the timer it
// wants to fire to be armed, which says the environment is waiting on it.
type Timers struct {
	mu      sync.Mutex
	armed   []*Timer
	changed chan struct{}
}

// NewTimers returns timers with none armed.
func NewTimers() *Timers { return &Timers{changed: make(chan struct{})} }

// Timer is one timeout an environment asked for.
type Timer struct {
	// Duration is what the environment asked for.
	Duration time.Duration

	once  sync.Once
	fired chan struct{}
}

// Fire fires the timer. Firing twice is the same as once.
func (t *Timer) Fire() { t.once.Do(func() { close(t.fired) }) }

// Timeout implements clock.Timers.
func (t *Timers) Timeout(d time.Duration) (<-chan struct{}, func()) {
	timer := &Timer{Duration: d, fired: make(chan struct{})}
	t.mu.Lock()
	t.armed = append(t.armed, timer)
	changed := t.changed
	t.changed = make(chan struct{})
	t.mu.Unlock()
	close(changed)
	return timer.fired, func() {}
}

// Await waits until n timers have been armed and returns the nth, counting from 1. It returns nil
// if ctx ends first.
func (t *Timers) Await(ctx context.Context, n int) *Timer {
	for {
		t.mu.Lock()
		if len(t.armed) >= n {
			timer := t.armed[n-1]
			t.mu.Unlock()
			return timer
		}
		changed := t.changed
		t.mu.Unlock()
		select {
		case <-changed:
		case <-ctx.Done():
			return nil
		}
	}
}

// Armed returns how many timers have been armed so far.
func (t *Timers) Armed() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return len(t.armed)
}
