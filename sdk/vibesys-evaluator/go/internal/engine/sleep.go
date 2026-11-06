package engine

import (
	"context"
	"time"
)

// The open loop's scheduler hands each operation to a sender ahead of its arrival time, with the
// Go timer, which wakes a sub-millisecond sleep about a millisecond late. The sender then waits
// for the exact time with waitUntil (sleep_linux.go: nanosleep, which wakes within the kernel's
// timer slack, 50 us by default, then a yield loop; sleep_other.go: the same shape with the Go
// timer, for the platforms the wheels also ship binaries for). The scheduler itself must not
// sleep in a system call: a sender it has just readied would wait on its processor until the
// runtime takes that processor back, which showed as milliseconds of lag.
const (
	ahead    = 2 * time.Millisecond
	slack    = 60 * time.Microsecond
	maxChunk = 10 * time.Millisecond
)

// sleepUntil waits for t with the Go timer, or until ctx ends.
func sleepUntil(ctx context.Context, t time.Time) {
	remaining := time.Until(t)
	if remaining <= 0 {
		return
	}
	timer := time.NewTimer(remaining)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}
