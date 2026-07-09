package main

import "testing"

func TestSelectedScenariosExpandsAll(t *testing.T) {
	scenarios, err := selectedScenarios("all")
	if err != nil {
		t.Fatal(err)
	}
	want := []scenario{scenarioSPSC, scenarioMPSC, scenarioMPMC}
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
	got := failureHistoryForScenario("failure.json", scenarioMPMC, 3)
	if got != "failure-mpmc.json" {
		t.Fatalf("failure history = %q, want %q", got, "failure-mpmc.json")
	}
	if got := failureHistoryForScenario("failure.json", scenarioMPMC, 1); got != "failure.json" {
		t.Fatalf("single-scenario failure history = %q", got)
	}
}
