// Command go-bench is a whole benchmark script in Go: the Go mirror of a Python task's
// `vibesys_evaluator.main(...)`. It measures how fast this machine hashes (three trials, their
// median) and checks that hashing is deterministic; it has no system under test, which is the
// point: Main, Task, Run, Combine and the sample files are all a domain-free task needs.
//
//	go-bench check
//	go-bench benchmark --vs-output records.jsonl [--seed N]
//	go-bench profile
package main

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
)

const (
	trials    = 3
	trialSecs = 0.2
	blockSize = 64 << 10
)

// hashesPerSecond hashes blocks for trialSecs and returns the rate in MB/s.
func hashesPerSecond(ctx context.Context) (float64, error) {
	block := make([]byte, blockSize)
	start, bytes := time.Now(), 0
	for time.Since(start).Seconds() < trialSecs {
		if err := ctx.Err(); err != nil {
			return 0, err
		}
		sha256.Sum256(block)
		bytes += blockSize
	}
	return float64(bytes) / 1e6 / time.Since(start).Seconds(), nil
}

func benchmark(ctx context.Context, run *evaluator.Run) error {
	rates := make([]float64, 0, trials)
	for trial := range trials {
		dir, err := run.Phase(fmt.Sprintf("trial-%d", trial))
		if err != nil {
			return err
		}
		rate, err := hashesPerSecond(ctx)
		if err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(dir, "rate.txt"), fmt.Appendf(nil, "%.1f MB/s\n", rate), 0o644); err != nil {
			return err
		}
		rates = append(rates, rate)
		// What was measured so far goes in the error record if a later trial fails.
		run.SetPartial(&evaluator.Partial{Name: "last_trial_mb_per_s", Value: rate, Direction: evaluator.Max, Unit: "MB/s"})
	}
	median, err := evaluator.Combine(evaluator.Median, rates)
	if err != nil {
		return err
	}
	return run.Report(map[string]float64{"hash_mb_per_s": median})
}

func check(context.Context, *evaluator.Run) error {
	block := make([]byte, blockSize)
	if sha256.Sum256(block) != sha256.Sum256(block) {
		return fmt.Errorf("hashing the same block twice gave two digests")
	}
	return nil
}

func main() {
	evaluator.Main(evaluator.Task{
		Metrics:   map[string]evaluator.Metric{"hash_mb_per_s": {Unit: "MB/s", Direction: evaluator.Max}},
		Benchmark: benchmark,
		Check:     check,
		Profile:   func(ctx context.Context, run *evaluator.Run) error { _, err := hashesPerSecond(ctx); return err },
	})
}
