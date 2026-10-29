package lifecycle

import (
	"sync"
	"time"
)

// Clock schedules expiry callbacks. Cancelling an alarm need not prevent a
// callback already in flight; Idle's generation check fences those callbacks.
type Clock interface {
	AfterFunc(time.Duration, func()) func()
}

type SystemClock struct{}

func (SystemClock) AfterFunc(delay time.Duration, fire func()) func() {
	timer := time.AfterFunc(delay, fire)
	return func() { timer.Stop() }
}

// Monitor serializes transport connection events with idle expiry. onIdle
// must not reenter the monitor; it is called once after an idle expiry wins.
type Monitor struct {
	mu      sync.Mutex
	state   Idle
	clock   Clock
	timeout time.Duration
	cancel  func()
	stopped bool
	onIdle  func()
}

func NewMonitor(timeout time.Duration, clock Clock, onIdle func()) *Monitor {
	m := &Monitor{timeout: timeout, clock: clock, onIdle: onIdle}
	m.arm()
	return m
}

func (m *Monitor) Opened() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.stopped {
		return false
	}
	m.cancel()
	m.state = m.state.Opened()
	return true
}

func (m *Monitor) Closed() {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.stopped {
		return
	}
	m.state = m.state.Closed()
	if m.state.Connections == 0 {
		m.arm()
	}
}

func (m *Monitor) Stop() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.stopped = true
	m.cancel()
}

func (m *Monitor) arm() {
	generation := m.state.Generation
	m.cancel = m.clock.AfterFunc(m.timeout, func() {
		m.mu.Lock()
		defer m.mu.Unlock()
		if !m.stopped && m.state.Expired(generation) {
			m.stopped = true
			m.onIdle()
		}
	})
}
