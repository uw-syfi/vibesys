package main

import "testing"

func TestSelectedScenariosExpandsAll(t *testing.T) {
	scenarios, err := selectedScenarios("all")
	if err != nil {
		t.Fatal(err)
	}
	want := []scenario{scenarioSWMR, scenarioMW, scenarioPointHeavy, scenarioRangeHeavy}
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
	got := failureHistoryForScenario("failure.json", scenarioPointHeavy, 4)
	if got != "failure-point-heavy.json" {
		t.Fatalf("failure history = %q, want %q", got, "failure-point-heavy.json")
	}
	if got := failureHistoryForScenario("failure.json", scenarioMW, 1); got != "failure.json" {
		t.Fatalf("single-scenario failure history = %q", got)
	}
}

func TestCandidateConfigValidatesCopiedSizes(t *testing.T) {
	workspace := t.TempDir()
	if _, err := parseCandidateConfig(
		workspace,
		"ordered-map-candidate.so",
		true,
		"swmr",
		0,
		8,
	); err == nil {
		t.Fatal("zero max key size was accepted")
	}
	if _, err := parseCandidateConfig(
		workspace,
		"ordered-map-candidate.so",
		true,
		"swmr",
		8,
		maxMapSize+1,
	); err == nil {
		t.Fatal("oversized copied value was accepted")
	}
	if _, err := parseCandidateConfig(
		workspace,
		"ordered-map-candidate.so",
		true,
		"mw",
		8,
		64,
	); err != nil {
		t.Fatal(err)
	}
}

func TestClientCountKeepsRequestedClients(t *testing.T) {
	for _, selected := range []scenario{scenarioSWMR, scenarioMW, scenarioPointHeavy, scenarioRangeHeavy} {
		got, err := clientCount(selected, 4)
		if err != nil {
			t.Fatal(err)
		}
		if got != 4 {
			t.Fatalf("%s clients = %d, want 4", selected, got)
		}
	}
}
