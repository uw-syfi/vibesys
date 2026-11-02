// Package clock declares Clock, the one way the daemon reads the time, so a test supplies the time
// it wants and never reads a real clock.
package clock

import (
	"context"
	"sync"
	"time"
)

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

// Sleep waits for d and reports true, or returns false as soon as ctx is done.
func Sleep(ctx context.Context, t Timers, d time.Duration) bool {
	fired, stop := t.Timeout(d)
	defer stop()
	select {
	case <-fired:
		return true
	case <-ctx.Done():
		return false
	}
}

// Graceful runs gentle and, if it has not returned after d, runs force, which must make it return.
// It returns once gentle has. It is for a graceful stop, which waits for whoever is on the other end
// and so must not be the only way to stop.
func Graceful(t Timers, d time.Duration, gentle, force func()) {
	fired, stop := t.Timeout(d)
	defer stop()
	done := make(chan struct{})
	var wg sync.WaitGroup
	defer wg.Wait()
	wg.Go(func() {
		defer close(done)
		gentle()
	})
	select {
	case <-done:
	case <-fired:
		force()
	}
}

// Deadline returns a context that also ends after d, measured by the timers. Its error is
// context.Canceled, as a context cannot be made to report otherwise, and context.Cause says
// context.DeadlineExceeded. Call the returned stop when done with it: it returns once nothing is
// left running for the deadline.
func Deadline(ctx context.Context, t Timers, d time.Duration) (context.Context, func()) {
	ctx, cancel := context.WithCancelCause(ctx)
	fired, stopTimer := t.Timeout(d)
	var wg sync.WaitGroup
	wg.Go(func() {
		select {
		case <-fired:
			cancel(context.DeadlineExceeded)
		case <-ctx.Done():
		}
	})
	return ctx, func() {
		stopTimer()
		cancel(nil)
		wg.Wait()
	}
}
