package engine

import (
	"context"
	"runtime"
	"syscall"
	"time"
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
