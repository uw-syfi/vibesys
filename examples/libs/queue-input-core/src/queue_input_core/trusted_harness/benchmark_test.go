package main

import (
	"testing"
	"time"
)

func TestNativeReferenceBenchmark(t *testing.T) {
	result, err := runNativeBenchmark(benchmarkConfig{
		candidateConfig: candidateConfig{
			workspace:    t.TempDir(),
			useReference: true,
			scenario:     scenarioSPSC,
			capacity:     32,
			valueSize:    64,
		},
		producers: 1,
		consumers: 1,
		duration:  20 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Enqueued == 0 || result.Dequeued == 0 || result.TotalOpsPerSec <= 0 {
		t.Fatalf("benchmark did not perform useful work: %+v", result)
	}
	if result.Attempts != result.Enqueued+result.Dropped+result.Dequeued+result.Empty {
		t.Fatalf("benchmark returned inconsistent counters: %+v", result)
	}
}

func TestNativeBenchmarkLoadsCandidateCABI(t *testing.T) {
	workspace := compileCandidateFixture(t, false)
	result, err := runNativeBenchmark(benchmarkConfig{
		candidateConfig: candidateConfig{
			workspace: workspace,
			candidate: "queue-candidate.so",
			scenario:  scenarioSPSC,
			capacity:  32,
			valueSize: 256,
		},
		producers: 1,
		consumers: 1,
		duration:  20 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Enqueued == 0 || result.Dequeued == 0 {
		t.Fatalf("candidate ABI benchmark did not transfer copied values: %+v", result)
	}
}
