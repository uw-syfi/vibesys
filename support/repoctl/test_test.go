package main

import (
	"context"
	"reflect"
	"testing"

	"repoctl/execution"
)

type recordingRunner struct{ checks []execution.Check }

func (r *recordingRunner) Run(_ context.Context, _ string, check execution.Check) error {
	r.checks = append(r.checks, check)
	return nil
}

func testFixture() (graph, plan) {
	g := graph{
		Components: map[string]component{
			"native:a": {ID: "native:a", Jobs: []string{"evaluators"}},
		},
		NativeTargets: map[string]nativeTarget{
			"native:a": {ComponentID: "native:a", Root: "apps/a", Language: "go", Selected: true},
		},
		NativeChecks:         nativeChecks{Commands: map[string][][]string{"go": {{"go", "test", "./..."}}}, TimeoutSeconds: 1},
		NativeCheckOverrides: map[string]nativeCheckOverride{},
		TestSuites: map[string]execution.Suite{
			"python": {Job: "python", Language: "python", Directory: ".", Commands: [][]string{{"uv", "run", "pytest"}}, TimeoutSeconds: 1},
			"tui":    {Job: "tui", Language: "typescript", Directory: "clients", Collection: "pnpm_packages", Commands: [][]string{{"pnpm", "test:clients"}}, PackageCommands: [][]string{{"pnpm", "--filter", "{package}", "test"}}, TimeoutSeconds: 1},
		},
	}
	p := plan{
		Jobs:        map[string]bool{"python": true, "tui": true, "evaluators": true},
		Components:  map[string][]string{"native:a": {"changed apps/a/main.go"}},
		Collections: map[string][]string{"pnpm_packages": {"@example/z", "@example/a"}},
	}
	return g, p
}

func TestSelectedTestChecksUsePackagesAndNativeTargets(t *testing.T) {
	g, p := testFixture()
	checks, targets, err := selectedTestChecks(g, p)
	if err != nil {
		t.Fatal(err)
	}
	want := [][]string{
		{"uv", "run", "pytest"},
		{"pnpm", "--filter", "@example/a", "test"},
		{"pnpm", "--filter", "@example/z", "test"},
	}
	got := make([][]string, 0, len(checks))
	for _, check := range checks {
		got = append(got, check.Args)
	}
	if !reflect.DeepEqual(got, want) || !reflect.DeepEqual(targets, []string{"apps/a"}) {
		t.Fatalf("commands = %v, targets = %v", got, targets)
	}
	delete(p.Collections, "pnpm_packages")
	checks, _, err = selectedTestChecks(g, p)
	if err != nil || !reflect.DeepEqual(checks[1].Args, []string{"pnpm", "test:clients"}) {
		t.Fatalf("fallback command = %v, err = %v", checks, err)
	}
}

func TestSelectedTestsRunAndDryRun(t *testing.T) {
	g, p := testFixture()
	runner := &recordingRunner{}
	nativeCalls := 0
	nativeRun := func(_ context.Context, _, _ string, _ []string, _ []string) error {
		nativeCalls++
		return nil
	}
	if err := runSelectedTests(t.TempDir(), g, p, true, runner, nativeRun); err != nil {
		t.Fatal(err)
	}
	if len(runner.checks) != 0 || nativeCalls != 0 {
		t.Fatalf("dry-run executed %d suite checks and %d native checks", len(runner.checks), nativeCalls)
	}
	if err := runSelectedTests(t.TempDir(), g, p, false, runner, nativeRun); err != nil {
		t.Fatal(err)
	}
	if len(runner.checks) != 3 || nativeCalls != 1 {
		t.Fatalf("executed %d suite checks and %d native checks", len(runner.checks), nativeCalls)
	}
}

func TestMissingSelectedSuiteFails(t *testing.T) {
	g, p := testFixture()
	delete(g.TestSuites, "python")
	if _, _, err := selectedTestChecks(g, p); err == nil {
		t.Fatal("missing selected suite accepted")
	}
}

func TestInvalidTestSuiteConfiguration(t *testing.T) {
	g, _ := testFixture()
	g.Jobs = []string{"python", "tui"}
	g.Collections = []collectionSpec{{Name: "pnpm_packages"}}
	tests := []execution.Suite{
		{Job: "unknown", Language: "python", Directory: ".", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}},
		{Job: "python", Language: "python", Directory: "../escape", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}},
		{Job: "python", Language: "python", Directory: ".", TimeoutSeconds: 0, Commands: [][]string{{"pytest"}}},
		{Job: "tui", Language: "typescript", Directory: "clients", Collection: "missing", TimeoutSeconds: 1, Commands: [][]string{{"pnpm"}}, PackageCommands: [][]string{{"pnpm", "{package}"}}},
		{Job: "python", Language: "python", Directory: ".", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}, Env: map[string]string{"BAD-KEY": "value"}},
	}
	for _, suite := range tests {
		g.TestSuites = map[string]execution.Suite{}
		if err := g.validateTestSuites([]execution.Suite{suite}); err == nil {
			t.Fatalf("accepted invalid suite %+v", suite)
		}
	}
}
