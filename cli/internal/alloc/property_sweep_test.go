//go:build !race

// This file is a sweep: it walks a large space of cases on one goroutine, so the race detector has
// nothing to check and only slows it down. check.sh runs it in its step without the detector.

package alloc_test

import "testing"

// Many more random histories than the race step runs, longer ones, each checked after every step
// against every invariant and the model.
func TestManyRandomHistoriesKeepEveryInvariant(t *testing.T) {
	histories(t, 600, 600)
}
