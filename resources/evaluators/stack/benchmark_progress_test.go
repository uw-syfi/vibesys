package main

import (
	"testing"
	"time"
)

// benchmarkHangGuard bounds how long a test waits for the native benchmark to exercise the
// candidate. It sits far above any passing run and is never what a test synchronizes on.
const benchmarkHangGuard = 30 * time.Second

// benchmarkUntilTransferred runs the native benchmark until a value has gone through the
// candidate (something enqueued and something dequeued), and returns that run.
//
// The measured window is wall-clock and the runner's threads are pinned to CPUs, so on a
// machine busy with other work a short window can pass without either thread running: the
// run is valid and its counters are zero. A test that asks "did a value transfer" waits for
// that, widening the window until it happened, and fails only if nothing transfers within
// benchmarkHangGuard, which means the candidate or runner is stuck rather than slow.
func benchmarkUntilTransferred(t *testing.T, config benchmarkConfig) benchmarkResult {
	t.Helper()
	var waited time.Duration
	var last benchmarkResult
	for window := config.duration; waited < benchmarkHangGuard; window *= 2 {
		config.duration = window
		result, err := runNativeBenchmark(config)
		if err != nil {
			t.Fatal(err)
		}
		if result.Enqueued > 0 && result.Dequeued > 0 {
			return result
		}
		last = result
		waited += window
	}
	t.Fatalf("no value transferred through the candidate within %s: %+v", benchmarkHangGuard, last)
	return last
}
