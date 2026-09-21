package main

import (
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestNativeBenchmarkTimesOutStuckCandidate(t *testing.T) {
	workspace := compileCandidateFixtureWithDefines(t, "VSOM_TEST_HANG_ON_PUT")
	previousGrace := nativeBenchmarkShutdownGrace
	nativeBenchmarkShutdownGrace = 100 * time.Millisecond
	t.Cleanup(func() { nativeBenchmarkShutdownGrace = previousGrace })

	_, err := runNativeBenchmark(benchmarkConfig{
		candidateConfig: candidateConfig{
			workspace:    workspace,
			candidate:    "ordered-map-candidate.so",
			scenario:     scenarioSWMR,
			maxKeySize:   8,
			maxValueSize: 8,
			clientCount:  1,
		},
		clients:  1,
		duration: 20 * time.Millisecond,
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
			scenario:     scenarioSWMR,
			maxKeySize:   8,
			maxValueSize: 8,
			clientCount:  2,
		},
		clients:  2,
		duration: 20 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Attempts == 0 || result.TotalOpsPerSec <= 0 {
		t.Fatalf("benchmark did not perform useful work: %+v", result)
	}
	completed := result.Puts + result.Gets + result.Removes + result.Successors + result.Ranges + result.Missing
	if result.Attempts != completed {
		t.Fatalf("benchmark returned inconsistent counters: %+v", result)
	}
}

func TestMedianBenchmarkResultPreservesMedianSampleAndAllRates(t *testing.T) {
	results := []benchmarkResult{
		{Scenario: "swmr", Puts: 10, TotalOpsPerSec: 30},
		{Scenario: "swmr", Puts: 20, TotalOpsPerSec: 10},
		{Scenario: "swmr", Puts: 30, TotalOpsPerSec: 20},
	}
	result := medianBenchmarkResult(results)
	if result.TotalOpsPerSec != 20 || result.Puts != 30 || result.Repetitions != 3 {
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
				scenario:     scenarioSWMR,
				maxKeySize:   8,
				maxValueSize: 8,
			},
			clients:     1,
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
			workspace:    workspace,
			candidate:    "ordered-map-candidate.so",
			scenario:     scenarioMW,
			maxKeySize:   8,
			maxValueSize: 8,
			clientCount:  2,
		},
		clients:  2,
		duration: 20 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Attempts == 0 {
		t.Fatalf("candidate ABI benchmark did not perform operations: %+v", result)
	}
}
