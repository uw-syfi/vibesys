package main

import (
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestNativeBenchmarkTimesOutStuckCandidate(t *testing.T) {
	workspace := compileCandidateFixtureWithDefines(t, "VSQ_TEST_HANG_CAPACITY_ONE")
	previousGrace := nativeBenchmarkShutdownGrace
	nativeBenchmarkShutdownGrace = 100 * time.Millisecond
	t.Cleanup(func() { nativeBenchmarkShutdownGrace = previousGrace })

	_, err := runNativeBenchmark(benchmarkConfig{
		candidateConfig: candidateConfig{
			workspace: workspace,
			candidate: "queue-candidate.so",
			scenario:  scenarioSPSC,
			capacity:  1,
			valueSize: 8,
		},
		producers: 1,
		consumers: 1,
		duration:  20 * time.Millisecond,
	})
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("stuck benchmark error = %v, want timeout", err)
	}
}

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

func TestMedianBenchmarkResultPreservesMedianSampleAndAllRates(t *testing.T) {
	results := []benchmarkResult{
		{Scenario: "spsc", Enqueued: 10, TotalOpsPerSec: 30},
		{Scenario: "spsc", Enqueued: 20, TotalOpsPerSec: 10},
		{Scenario: "spsc", Enqueued: 30, TotalOpsPerSec: 20},
	}
	result := medianBenchmarkResult(results)
	if result.TotalOpsPerSec != 20 || result.Enqueued != 30 || result.Repetitions != 3 {
		t.Fatalf("median result = %+v", result)
	}
	wantRates := []float64{30, 10, 20}
	if !reflect.DeepEqual(result.TotalOpsPerSecSamples, wantRates) {
		t.Fatalf("sample rates = %v, want %v", result.TotalOpsPerSecSamples, wantRates)
	}
}

func TestBenchmarkRejectsNonPositiveOrEvenRepetitions(t *testing.T) {
	for _, repetitions := range []int{0, 2} {
		_, err := runBenchmark(benchmarkConfig{
			candidateConfig: candidateConfig{
				workspace:    t.TempDir(),
				useReference: true,
				scenario:     scenarioSPSC,
				capacity:     1,
				valueSize:    8,
			},
			producers:   1,
			consumers:   1,
			duration:    time.Millisecond,
			repetitions: repetitions,
		})
		if err == nil {
			t.Fatalf("repetitions=%d unexpectedly passed", repetitions)
		}
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
