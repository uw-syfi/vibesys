// Package clock declares Clock, the one way the daemon reads the time, so a test supplies the time
// it wants and never reads a real clock.
package clock

import "time"

// Clock tells the time. The daemon stamps submissions, notes and state changes from it.
type Clock interface {
	Now() time.Time
}

// System reads the operating system's clock. It is the production Clock and Timers.
type System struct{}

// Now implements Clock.
func (System) Now() time.Time { return time.Now() }

// Timers makes the timeouts a caller asks for, such as `wait --timeout`. A test supplies timers
// it fires itself, so no test result depends on how long anything took.
type Timers interface {
	// Timeout returns a channel that is closed after d, and a stop that releases the timer when
	// the caller no longer needs it. Stop may be called more than once.
	Timeout(d time.Duration) (fired <-chan struct{}, stop func())
}

// Timeout implements Timers.
func (System) Timeout(d time.Duration) (<-chan struct{}, func()) {
	fired := make(chan struct{})
	t := time.AfterFunc(d, func() { close(fired) })
	return fired, func() { t.Stop() }
}
