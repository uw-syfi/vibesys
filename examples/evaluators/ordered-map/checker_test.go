package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/anishathalye/porcupine"
)

func TestCloseTimesOutStuckDestructor(t *testing.T) {
	workspace := compileCandidateFixtureWithDefines(t, "VSOM_TEST_HANG_ON_DESTROY")
	previousTimeout := candidateShutdownTimeout
	candidateShutdownTimeout = 100 * time.Millisecond
	t.Cleanup(func() { candidateShutdownTimeout = previousTimeout })

	session, err := startCandidate(candidateConfig{
		workspace:    workspace,
		candidate:    "ordered-map-candidate.so",
		scenario:     scenarioSWMR,
		maxKeySize:   8,
		maxValueSize: 8,
		clientCount:  1,
		laneCount:    1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := session.invoke(0, request{
		operation: operationPut,
		key:       []byte{1},
		value:     []byte{1},
	}); err != nil {
		_ = session.close()
		t.Fatal(err)
	}

	started := time.Now()
	err = session.close()
	if err == nil || !strings.Contains(err.Error(), "shutdown timed out") {
		t.Fatalf("stuck destructor error = %v, want shutdown timeout", err)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("stuck destructor took %s to reject", elapsed)
	}
}

func TestAccuracyTimesOutStuckCandidateOperation(t *testing.T) {
	workspace := compileCandidateFixtureWithDefines(t, "VSOM_TEST_HANG_ON_PUT")
	previousTimeout := candidateOperationTimeout
	candidateOperationTimeout = 100 * time.Millisecond
	t.Cleanup(func() { candidateOperationTimeout = previousTimeout })

	started := time.Now()
	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:    workspace,
			candidate:    "ordered-map-candidate.so",
			scenario:     scenarioSWMR,
			maxKeySize:   8,
			maxValueSize: 8,
			clientCount:  2,
		},
		operations:  8,
		trials:      1,
		clients:     2,
		seed:        7,
		checkBudget: defaultCheckBudget,
	})
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("stuck candidate error = %v, want operation timeout", err)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("stuck candidate took %s to reject", elapsed)
	}
}

func TestAccuracyRejectsUnboundedCheckBudget(t *testing.T) {
	for _, budget := range []time.Duration{0, -time.Second} {
		err := runAccuracy(accuracyConfig{
			candidateConfig: candidateConfig{
				workspace:    t.TempDir(),
				useReference: true,
				scenario:     scenarioSWMR,
				maxKeySize:   8,
				maxValueSize: 8,
			},
			operations:  8,
			trials:      1,
			clients:     1,
			seed:        7,
			checkBudget: budget,
		})
		if err == nil || !strings.Contains(err.Error(), "check budget") {
			t.Fatalf("check budget %s error = %v, want a rejection", budget, err)
		}
	}
}

func TestAnnotateWeakOrderedOpsMarksOverlappingRange(t *testing.T) {
	put := putRecord(0, "a", "1", 1)
	put.Return = 8
	rng := rangeRecord(1, "a", "d", 4, nil, false, 3)
	rng.Return = 5
	min := minRecord(2, "a", "1", 10)
	history := []recordedOperation{put, rng, min}
	annotateWeakOrderedOps(history)
	if !history[1].Input.Weak {
		t.Fatal("range overlapping a put was not marked weak")
	}
	if history[2].Input.Weak {
		t.Fatal("min after the put returned was marked weak")
	}
	if history[0].Input.Weak {
		t.Fatal("put was marked weak")
	}
}

func TestGateFailureRejectsUndecidedHistory(t *testing.T) {
	config := accuracyConfig{
		candidateConfig: candidateConfig{scenario: scenarioMW},
		checkBudget:     250 * time.Millisecond,
	}
	if err := gateFailure(porcupine.Ok, config, "trial 0"); err != nil {
		t.Fatalf("decided-linearizable history failed the gate: %v", err)
	}
	if err := gateFailure(porcupine.Illegal, config, "trial 0"); err == nil ||
		!strings.Contains(err.Error(), "violates") {
		t.Fatalf("illegal history error = %v, want a violation", err)
	}
	err := gateFailure(porcupine.Unknown, config, "trial 0")
	if err == nil {
		t.Fatal("undecided history passed the correctness gate")
	}
	for _, want := range []string{"trial 0", "could not be decided", "mw", "250ms"} {
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("undecided history error = %v, want it to name %q", err, want)
		}
	}
	if strings.Contains(err.Error(), "violates") {
		t.Fatalf("undecided history error = %v, want no claimed violation", err)
	}
}

func compileCandidateFixture(t *testing.T, retainInput bool) string {
	defines := []string{}
	if retainInput {
		defines = append(defines, "VSOM_TEST_RETAIN_INPUT")
	}
	return compileCandidateFixtureWithDefines(t, defines...)
}

func compileCandidateFixtureWithDefines(t *testing.T, defines ...string) string {
	t.Helper()
	evaluatorSource, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	workspace := t.TempDir()
	library := filepath.Join(workspace, "ordered-map-candidate.so")
	include := filepath.Join(evaluatorSource, "include")
	source := filepath.Join(evaluatorSource, "testdata", "abi_test_candidate.c")
	args := []string{"-std=c11", "-O2", "-pthread", "-I", include, source, "-o", library}
	for _, define := range defines {
		args = append([]string{"-D" + define}, args...)
	}
	if runtime.GOOS == "darwin" {
		args = append([]string{"-dynamiclib"}, args...)
	} else {
		args = append([]string{"-shared", "-fPIC"}, args...)
	}
	command := exec.Command("cc", args...)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("compile candidate fixture: %v\n%s", err, output)
	}
	return workspace
}

func TestAccuracyRejectsCandidateThatOnlySupportsMaximumLength(t *testing.T) {
	workspace := compileCandidateFixtureWithDefines(t, "VSOM_TEST_FIXED_LENGTH_ONLY")
	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:    workspace,
			candidate:    "ordered-map-candidate.so",
			scenario:     scenarioSWMR,
			maxKeySize:   8,
			maxValueSize: 8,
			clientCount:  1,
		},
		operations:  8,
		trials:      1,
		clients:     1,
		seed:        7,
		checkBudget: defaultCheckBudget,
	})
	if err == nil {
		t.Fatal("candidate that only supports maximum-length values passed ABI probes")
	}
}

func TestAccuracyUsesCopyingCABI(t *testing.T) {
	workspace := compileCandidateFixture(t, false)

	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:    workspace,
			candidate:    "ordered-map-candidate.so",
			scenario:     scenarioMW,
			maxKeySize:   8,
			maxValueSize: 8,
			clientCount:  2,
		},
		operations:  16,
		trials:      1,
		clients:     2,
		seed:        7,
		checkBudget: defaultCheckBudget,
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestAccuracyRejectsCandidateThatRetainsPutInput(t *testing.T) {
	workspace := compileCandidateFixture(t, true)
	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:    workspace,
			candidate:    "ordered-map-candidate.so",
			scenario:     scenarioSWMR,
			maxKeySize:   8,
			maxValueSize: 8,
			clientCount:  1,
		},
		operations:  8,
		trials:      1,
		clients:     1,
		seed:        7,
		checkBudget: defaultCheckBudget,
	})
	if err == nil {
		t.Fatal("candidate that retained put input passed copying ABI checks")
	}
}

func TestAccuracyReferenceLinearizableHistories(t *testing.T) {
	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:    t.TempDir(),
			useReference: true,
			scenario:     scenarioSWMR,
			maxKeySize:   8,
			maxValueSize: 8,
			clientCount:  2,
		},
		operations:  24,
		trials:      2,
		clients:     2,
		seed:        11,
		checkBudget: defaultCheckBudget,
	})
	if err != nil {
		t.Fatal(err)
	}
}
