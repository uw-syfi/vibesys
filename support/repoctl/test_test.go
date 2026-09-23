package main

import (
	"context"
	"reflect"
	"strings"
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
			"native:a":  {ID: "native:a", Jobs: []string{"evaluators"}},
			"package:a": {ID: "package:a", Class: "workspace_package", Name: "@example/a"},
			"package:z": {ID: "package:z", Class: "workspace_package", Name: "@example/z"},
		},
		Order:       []string{"native:a", "package:a", "package:z"},
		Collections: []collectionSpec{{Name: "pnpm_packages", Class: "workspace_package", Field: "name"}},
		NativeTargets: map[string]nativeTarget{
			"native:a": {ComponentID: "native:a", Root: "apps/a", Language: "go", Selected: true},
		},
		NativeChecks:         nativeChecks{Commands: map[string][][]string{"go": {{"go", "test", "./..."}}}, TimeoutSeconds: 1},
		NativeCheckOverrides: map[string]nativeCheckOverride{},
		CheckGroups: map[string]execution.Suite{
			"python": {Name: "python", TriggerJob: "python", IncludeInTest: true, Language: "python", Directory: ".", Commands: [][]string{{"uv", "run", "pytest"}}, TimeoutSeconds: 1},
			"tui":    {Name: "tui", TriggerJob: "tui", IncludeInTest: true, Language: "typescript", Directory: "clients", Collection: "pnpm_packages", Commands: [][]string{{"pnpm", "test:clients"}}, PackageCommands: [][]string{{"pnpm", "--filter", "{package}", "test"}}, TimeoutSeconds: 1},
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
	delete(g.CheckGroups, "python")
	if _, _, err := selectedTestChecks(g, p); err == nil {
		t.Fatal("missing selected suite accepted")
	}
}

func TestInvalidTestSuiteConfiguration(t *testing.T) {
	g, _ := testFixture()
	g.Jobs = []string{"python", "tui"}
	g.Collections = []collectionSpec{{Name: "pnpm_packages"}}
	tests := []execution.Suite{
		{Name: "unknown", TriggerJob: "unknown", IncludeInTest: true, Language: "python", Directory: ".", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}},
		{Name: "python", Language: "python", Directory: "../escape", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}},
		{Name: "python", Language: "python", Directory: ".", TimeoutSeconds: 0, Commands: [][]string{{"pytest"}}},
		{Name: "tui", Language: "typescript", Directory: "clients", Collection: "missing", TimeoutSeconds: 1, Commands: [][]string{{"pnpm"}}, PackageCommands: [][]string{{"pnpm", "{package}"}}},
		{Name: "python", Language: "python", Directory: ".", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}, Env: map[string]string{"BAD-KEY": "value"}},
	}
	for _, suite := range tests {
		g.CheckGroups = map[string]execution.Suite{}
		if err := g.validateCheckGroups([]execution.Suite{suite}); err == nil {
			t.Fatalf("accepted invalid suite %+v", suite)
		}
	}
}

func TestRunCheckGroupRejectsUnknownAndInvalidCollections(t *testing.T) {
	g, _ := testFixture()
	for _, tc := range []struct {
		name, group, collection, message string
	}{
		{"unknown group", "missing", "", "unknown check group"},
		{"unexpected selection", "python", `["@example/a"]`, "does not use a collection"},
		{"malformed selection", "tui", `{"name":"@example/a"}`, "JSON array"},
		{"unregistered selection", "tui", `["@example/missing"]`, "unregistered collection value"},
		{"duplicate selection", "tui", `["@example/a","@example/a"]`, "duplicate collection value"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			runner := &recordingRunner{}
			err := runCheckGroup(t.TempDir(), g, tc.group, tc.collection, false, runner)
			if err == nil || !strings.Contains(err.Error(), tc.message) {
				t.Fatalf("error = %v, want %q", err, tc.message)
			}
			if len(runner.checks) != 0 {
				t.Fatalf("executed checks: %+v", runner.checks)
			}
		})
	}
}

func TestRunCheckGroupUsesAlwaysAndSelectedPackageCommands(t *testing.T) {
	g, _ := testFixture()
	tui := g.CheckGroups["tui"]
	tui.AlwaysCommands = [][]string{{"pnpm", "check:ts"}}
	g.CheckGroups["tui"] = tui
	runner := &recordingRunner{}
	if err := runCheckGroup(t.TempDir(), g, "tui", `["@example/z"]`, false, runner); err != nil {
		t.Fatal(err)
	}
	want := [][]string{{"pnpm", "check:ts"}, {"pnpm", "--filter", "@example/z", "test"}}
	got := [][]string{runner.checks[0].Args, runner.checks[1].Args}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("commands = %v, want %v", got, want)
	}
}

func TestSelectedJobWithoutRunnableChecksFails(t *testing.T) {
	g, p := testFixture()
	delete(g.CheckGroups, "python")
	if _, _, err := selectedTestChecks(g, p); err == nil || !strings.Contains(err.Error(), `selected job "python"`) {
		t.Fatalf("error = %v", err)
	}
	delete(p.Components, "native:a")
	delete(p.Jobs, "python")
	if _, _, err := selectedTestChecks(g, p); err == nil || !strings.Contains(err.Error(), `selected job "evaluators"`) {
		t.Fatalf("error = %v", err)
	}
}

func TestPolicyRejectsJobWithoutRunnableChecks(t *testing.T) {
	g, _ := testFixture()
	g.Jobs = []string{"python", "tui", "evaluators", "orphan"}
	if err := g.validateRunnableJobs(); err == nil || !strings.Contains(err.Error(), `job "orphan"`) {
		t.Fatalf("error = %v", err)
	}
	g.CheckGroups["orphan"] = execution.Suite{Name: "orphan", TriggerJob: "orphan"}
	if err := g.validateRunnableJobs(); err != nil {
		t.Fatal(err)
	}
}
