//go:build !linux

package engine

import (
	"context"
	"runtime"
	"time"
)

// waitUntil returns at t or as soon as ctx ends. Without nanosleep it sleeps with the Go timer
// until slack before t, then yields until it is due: the open loop is exact on Linux, the
// platform the benchmarks run on, and merely works elsewhere.
func waitUntil(ctx context.Context, t time.Time) {
	for ctx.Err() == nil {
		remaining := time.Until(t)
		switch {
		case remaining <= 0:
			return
		case remaining > 2*time.Millisecond:
			time.Sleep(min(remaining-time.Millisecond, maxChunk))
		default:
			runtime.Gosched()
		}
	}
}
