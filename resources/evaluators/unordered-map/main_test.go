package main

import "testing"

func TestSelectedScenariosExpandsAll(t *testing.T) {
	scenarios, err := selectedScenarios("all")
	if err != nil {
		t.Fatal(err)
	}
	want := []scenario{scenarioSWMR, scenarioMW}
	if len(scenarios) != len(want) {
		t.Fatalf("scenarios = %v, want %v", scenarios, want)
	}
	for index := range want {
		if scenarios[index] != want[index] {
			t.Fatalf("scenarios = %v, want %v", scenarios, want)
		}
	}
}

func TestFailureHistoryGetsScenarioSuffixForAll(t *testing.T) {
	got := failureHistoryForScenario("failure.json", scenarioMW, 2)
	if got != "failure-mw.json" {
		t.Fatalf("failure history = %q, want %q", got, "failure-mw.json")
	}
	if got := failureHistoryForScenario("failure.json", scenarioMW, 1); got != "failure.json" {
		t.Fatalf("single-scenario failure history = %q", got)
	}
}

func TestCandidateConfigValidatesCopiedSizes(t *testing.T) {
	workspace := t.TempDir()
	if _, err := parseCandidateConfig(
		workspace,
		"unordered-map-candidate.so",
		true,
		"swmr",
		minMapPayloadSize-1,
		64,
		4,
	); err == nil {
		t.Fatal("undersized copied key was accepted")
	}
	if _, err := parseCandidateConfig(
		workspace,
		"unordered-map-candidate.so",
		true,
		"mw",
		64,
		maxMapValueSize+1,
		4,
	); err == nil {
		t.Fatal("oversized copied value was accepted")
	}
	if _, err := parseCandidateConfig(
		workspace,
		"unordered-map-candidate.so",
		true,
		"swmr",
		64,
		64,
		4,
	); err != nil {
		t.Fatal(err)
	}
}

func TestClientCountForScenarioRequiresPositiveCount(t *testing.T) {
	if _, err := clientCountForScenario(scenarioSWMR, 0); err == nil {
		t.Fatal("zero clients were accepted")
	}
	got, err := clientCountForScenario(scenarioMW, 4)
	if err != nil || got != 4 {
		t.Fatalf("client count = %d, %v", got, err)
	}
}
