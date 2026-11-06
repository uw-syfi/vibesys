package k8s

import (
	"context"
	"sync"
	"time"
)

// Clock is the clock the deploy layer waits with: SystemClock, or a FakeClock in tests.
type Clock interface {
	// Now is the time; differences of two readings are deadlines.
	Now() time.Time
	// Sleep waits d, or returns the context's error when it ends first.
	Sleep(ctx context.Context, d time.Duration) error
}

// SystemClock is the real time.
type SystemClock struct{}

// Now implements Clock.
func (SystemClock) Now() time.Time { return time.Now() }

// Sleep implements Clock.
func (SystemClock) Sleep(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// FakeClock is virtual time: Sleep advances it at once.
type FakeClock struct {
	mu  sync.Mutex
	now time.Time
}

// NewFakeClock starts a FakeClock at an arbitrary fixed time.
func NewFakeClock() *FakeClock { return &FakeClock{now: time.Unix(1_800_000_000, 0)} }

// Now implements Clock.
func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// Sleep implements Clock.
func (c *FakeClock) Sleep(ctx context.Context, d time.Duration) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	c.mu.Lock()
	c.now = c.now.Add(max(d, 0))
	c.mu.Unlock()
	return nil
}
