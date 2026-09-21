package main

import (
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestNativeBenchmarkTimesOutStuckCandidate(t *testing.T) {
	workspace := compileCandidateFixtureWithDefines(t, "VSUM_TEST_HANG_SINGLE_CLIENT")
	previousGrace := nativeBenchmarkShutdownGrace
	nativeBenchmarkShutdownGrace = 100 * time.Millisecond
	t.Cleanup(func() { nativeBenchmarkShutdownGrace = previousGrace })

	_, err := runNativeBenchmark(benchmarkConfig{
		candidateConfig: candidateConfig{
			workspace:   workspace,
			candidate:   "unordered-map-candidate.so",
			scenario:    scenarioSWMR,
			keySize:     8,
			valueSize:   8,
			clientCount: 1,
		},
		duration: 20 * time.Millisecond,
		keySpace: 16,
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
			keySize:      64,
			valueSize:    64,
			clientCount:  4,
		},
		duration: 20 * time.Millisecond,
		keySpace: 32,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Puts+result.Gets+result.Removes == 0 || result.TotalOpsPerSec <= 0 {
		t.Fatalf("benchmark did not perform useful work: %+v", result)
	}
	if result.Hits+result.Misses != result.Gets+result.Removes {
		t.Fatalf("benchmark returned inconsistent counters: %+v", result)
	}
}

func TestNativeReferenceMixedBenchmark(t *testing.T) {
	result, err := runNativeBenchmark(benchmarkConfig{
		candidateConfig: candidateConfig{
			workspace:    t.TempDir(),
			useReference: true,
			scenario:     scenarioMW,
			keySize:      32,
			valueSize:    32,
			clientCount:  4,
		},
		duration: 20 * time.Millisecond,
		keySpace: 64,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Puts == 0 || result.Gets == 0 || result.Removes == 0 {
		t.Fatalf("mixed benchmark did not exercise put/get/remove: %+v", result)
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
				keySize:      8,
				valueSize:    8,
				clientCount:  1,
			},
			duration:    time.Millisecond,
			repetitions: repetitions,
			keySpace:    8,
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
			workspace:   workspace,
			candidate:   "unordered-map-candidate.so",
			scenario:    scenarioSWMR,
			keySize:     32,
			valueSize:   256,
			clientCount: 2,
		},
		duration: 20 * time.Millisecond,
		keySpace: 32,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Puts+result.Gets == 0 {
		t.Fatalf("candidate ABI benchmark did not copy keys and values: %+v", result)
	}
}
