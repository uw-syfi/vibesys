package main

import (
	"testing"
	"time"
)

// benchmarkHangGuard bounds how long a test waits for the native benchmark to exercise the
// candidate. It sits far above any passing run and is never what a test synchronizes on.
const benchmarkHangGuard = 30 * time.Second

// benchmarkUntilWorked runs the native benchmark until the workload ran (by default, until
// some operation completed; exercised replaces that test), and returns that run.
//
// The measured window is wall-clock and the runner's threads are pinned to CPUs, so on a
// machine busy with other work a short window can pass without any thread running: the run is
// valid and its counters are zero. A test that asks "did the workload run" waits for that,
// widening the window until it did, and fails only if nothing ran within benchmarkHangGuard,
// which means the candidate or runner is stuck rather than slow.
func benchmarkUntilWorked(t *testing.T, config benchmarkConfig, exercised ...func(benchmarkResult) bool) benchmarkResult {
	t.Helper()
	worked := func(r benchmarkResult) bool { return r.Attempts > 0 }
	if len(exercised) > 0 {
		worked = exercised[0]
	}
	var waited time.Duration
	var last benchmarkResult
	for window := config.duration; waited < benchmarkHangGuard; window *= 2 {
		config.duration = window
		result, err := runNativeBenchmark(config)
		if err != nil {
			t.Fatal(err)
		}
		if worked(result) {
			return result
		}
		last = result
		waited += window
	}
	t.Fatalf("the workload did not run within %s: %+v", benchmarkHangGuard, last)
	return last
}
