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
	workspace := compileCandidateFixtureWithDefines(t, "VSUM_TEST_HANG_ON_DESTROY")
	previousTimeout := candidateShutdownTimeout
	candidateShutdownTimeout = 100 * time.Millisecond
	t.Cleanup(func() { candidateShutdownTimeout = previousTimeout })

	session, err := startCandidate(candidateConfig{
		workspace:   workspace,
		candidate:   "unordered-map-candidate.so",
		scenario:    scenarioSWMR,
		keySize:     64,
		valueSize:   64,
		clientCount: 1,
		laneCount:   1,
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
	workspace := compileCandidateFixtureWithDefines(t, "VSUM_TEST_HANG_SINGLE_CLIENT")
	previousTimeout := candidateOperationTimeout
	candidateOperationTimeout = 100 * time.Millisecond
	t.Cleanup(func() { candidateOperationTimeout = previousTimeout })

	started := time.Now()
	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:   workspace,
			candidate:   "unordered-map-candidate.so",
			scenario:    scenarioSWMR,
			keySize:     64,
			valueSize:   64,
			clientCount: 4,
		},
		operations:  8,
		trials:      1,
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

// TestAccuracyRejectsUnboundedCheckBudget pins the budget as mandatory:
// porcupine reads a zero timeout as unlimited, which is the hang the budget
// exists to prevent.
func TestAccuracyRejectsUnboundedCheckBudget(t *testing.T) {
	for _, budget := range []time.Duration{0, -time.Second} {
		err := runAccuracy(accuracyConfig{
			candidateConfig: candidateConfig{
				workspace:    t.TempDir(),
				useReference: true,
				scenario:     scenarioSWMR,
				keySize:      64,
				valueSize:    64,
				clientCount:  4,
			},
			operations:  8,
			trials:      1,
			seed:        7,
			checkBudget: budget,
		})
		if err == nil || !strings.Contains(err.Error(), "check budget") {
			t.Fatalf("check budget %s error = %v, want a rejection", budget, err)
		}
	}
}

// TestGateFailureRejectsUndecidedHistory covers the verdict a bounded check
// adds. A candidate must not win by making the checker slow, so an undecided
// history fails the gate with a reason that names the scenario and the budget
// instead of claiming a violation nobody proved.
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

func TestMapOutputRejectsWrongOperationStatus(t *testing.T) {
	_, err := mapOutputFor(
		request{operation: operationPut, key: []byte("a"), value: []byte("v")},
		response{status: statusMissing},
	)
	if err == nil {
		t.Fatal("put accepted a missing response status")
	}

	_, err = mapOutputFor(
		request{operation: operationGet, key: []byte("a")},
		response{status: statusInvalid},
	)
	if err == nil {
		t.Fatal("get accepted an invalid response status")
	}
}

func compileCandidateFixture(t *testing.T, retainInput bool) string {
	defines := []string{}
	if retainInput {
		defines = append(defines, "VSUM_TEST_RETAIN_INPUT")
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
	library := filepath.Join(workspace, "unordered-map-candidate.so")
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
	workspace := compileCandidateFixtureWithDefines(t, "VSUM_TEST_FIXED_LENGTH_ONLY")
	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:   workspace,
			candidate:   "unordered-map-candidate.so",
			scenario:    scenarioSWMR,
			keySize:     64,
			valueSize:   64,
			clientCount: 4,
		},
		operations:  8,
		trials:      1,
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
			workspace:   workspace,
			candidate:   "unordered-map-candidate.so",
			scenario:    scenarioMW,
			keySize:     64,
			valueSize:   64,
			clientCount: 4,
		},
		operations:  16,
		trials:      1,
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
			workspace:   workspace,
			candidate:   "unordered-map-candidate.so",
			scenario:    scenarioSWMR,
			keySize:     64,
			valueSize:   64,
			clientCount: 4,
		},
		operations:  8,
		trials:      1,
		seed:        7,
		checkBudget: defaultCheckBudget,
	})
	if err == nil {
		t.Fatal("candidate that retained put input passed copying ABI checks")
	}
}

func TestAccuracyRejectsCandidateThatClobbersMissingOutput(t *testing.T) {
	workspace := compileCandidateFixtureWithDefines(t, "VSUM_TEST_CLOBBER_MISSING")
	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:   workspace,
			candidate:   "unordered-map-candidate.so",
			scenario:    scenarioSWMR,
			keySize:     64,
			valueSize:   64,
			clientCount: 4,
		},
		operations:  8,
		trials:      1,
		seed:        7,
		checkBudget: defaultCheckBudget,
	})
	if err == nil {
		t.Fatal("candidate that clobbered missing output passed ABI probes")
	}
}

func TestAccuracyRejectsCandidateThatRemovesOnUndersizedOutput(t *testing.T) {
	workspace := compileCandidateFixtureWithDefines(t, "VSUM_TEST_REMOVE_ON_UNDERSIZED")
	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:   workspace,
			candidate:   "unordered-map-candidate.so",
			scenario:    scenarioSWMR,
			keySize:     64,
			valueSize:   64,
			clientCount: 4,
		},
		operations:  8,
		trials:      1,
		seed:        7,
		checkBudget: defaultCheckBudget,
	})
	if err == nil {
		t.Fatal("candidate that dropped mappings on undersized remove passed ABI probes")
	}
}

func TestAccuracyReferenceHistories(t *testing.T) {
	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:    t.TempDir(),
			useReference: true,
			scenario:     scenarioSWMR,
			keySize:      32,
			valueSize:    64,
			clientCount:  4,
		},
		operations:  12,
		trials:      2,
		seed:        3,
		checkBudget: defaultCheckBudget,
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestAccuracyReferenceMixedWriterHistories(t *testing.T) {
	err := runAccuracy(accuracyConfig{
		candidateConfig: candidateConfig{
			workspace:    t.TempDir(),
			useReference: true,
			scenario:     scenarioMW,
			keySize:      32,
			valueSize:    32,
			clientCount:  4,
		},
		operations:  16,
		trials:      1,
		seed:        11,
		checkBudget: defaultCheckBudget,
	})
	if err != nil {
		t.Fatal(err)
	}
}
