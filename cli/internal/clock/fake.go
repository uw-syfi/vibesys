package clock

import (
	"sync"
	"time"
)

// Manual is a Clock whose time moves only when a test says so. It is safe for concurrent use.
// Every reading is later than the one before, as the system clock's are, so two events a test
// stamps never share a time unless the test sets it.
type Manual struct {
	mu   sync.Mutex
	now  time.Time
	tick time.Duration
}

// NewManual returns a Manual that starts at start and moves forward by tick on every reading. A
// tick of zero holds the time still.
func NewManual(start time.Time, tick time.Duration) *Manual {
	return &Manual{now: start, tick: tick}
}

// Now implements Clock.
func (m *Manual) Now() time.Time {
	m.mu.Lock()
	defer m.mu.Unlock()
	t := m.now
	m.now = m.now.Add(m.tick)
	return t
}

// Advance moves the time forward.
func (m *Manual) Advance(d time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.now = m.now.Add(d)
}

// ManualTimers is Timers that fire only when a test says so.
type ManualTimers struct {
	mu      sync.Mutex
	pending []chan struct{}
	armed   chan time.Duration
}

// NewManualTimers returns timers with no timeout armed.
func NewManualTimers() *ManualTimers {
	return &ManualTimers{armed: make(chan time.Duration, 64)}
}

// Timeout implements Timers. The duration is reported on Armed, and the timer waits for Fire.
func (m *ManualTimers) Timeout(d time.Duration) (<-chan struct{}, func()) {
	fired := make(chan struct{})
	m.mu.Lock()
	m.pending = append(m.pending, fired)
	m.mu.Unlock()
	m.armed <- d
	return fired, func() {}
}

// Armed receives the duration of each timeout asked for, in order. A test that must fire a
// timeout waits on it first, which says the timeout exists to be fired.
func (m *ManualTimers) Armed() <-chan time.Duration { return m.armed }

// Fire closes every timeout armed so far.
func (m *ManualTimers) Fire() {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, c := range m.pending {
		close(c)
	}
	m.pending = nil
}
