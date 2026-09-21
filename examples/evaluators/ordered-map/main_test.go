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
	for _, selected := range []scenario{scenarioSWMR, scenarioMW} {
		got, err := clientCount(selected, 4)
		if err != nil {
			t.Fatal(err)
		}
		if got != 4 {
			t.Fatalf("%s clients = %d, want 4", selected, got)
		}
	}
}
