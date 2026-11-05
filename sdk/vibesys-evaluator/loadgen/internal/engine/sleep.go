package engine

import (
	"context"
	"runtime"
	"syscall"
	"time"
)

// The open loop's scheduler hands each operation to a sender ahead of its arrival time, with the
// Go timer, which wakes a sub-millisecond sleep about a millisecond late. The sender then waits
// for the exact time with nanosleep, which wakes within the kernel's timer slack (50 us by
// default): it sleeps until slack before the arrival, then yields until it is due. The scheduler
// itself must not sleep in a system call: a sender it has just readied would wait on its
// processor until the runtime takes that processor back, which showed as milliseconds of lag.
const (
	ahead    = 2 * time.Millisecond
	slack    = 60 * time.Microsecond
	maxChunk = 10 * time.Millisecond
)

// waitUntil returns at t or as soon as ctx ends.
func waitUntil(ctx context.Context, t time.Time) {
	for ctx.Err() == nil {
		remaining := time.Until(t)
		switch {
		case remaining <= 0:
			return
		case remaining > slack:
			ts := syscall.NsecToTimespec(int64(min(remaining-slack, maxChunk)))
			_ = syscall.Nanosleep(&ts, nil)
		default:
			runtime.Gosched()
		}
	}
}

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
