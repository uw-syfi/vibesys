package main

import (
	"context"
	"fmt"
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
			"python":  {Name: "python", TriggerJob: "python", IncludeInTest: true, Language: "python", Directory: ".", Commands: [][]string{{"uv", "run", "pytest"}}, TimeoutSeconds: 1},
			"clients": {Name: "clients", TriggerJob: "clients", IncludeInTest: true, Language: "typescript", Directory: "clients", Collection: "pnpm_packages", Commands: [][]string{{"pnpm", "test:clients"}}, PackageCommands: [][]string{{"pnpm", "--filter", "{package}", "test"}}, TimeoutSeconds: 1},
		},
	}
	p := plan{
		Jobs:        map[string]bool{"python": true, "clients": true, "evaluators": true},
		Components:  map[string][]string{"native:a": {"changed apps/a/main.go"}},
		Collections: map[string][]string{"pnpm_packages": {"@example/z", "@example/a"}},
	}
	return g, p
}

func TestSelectedTestChecksUsePackagesAndNativeTargets(t *testing.T) {
	g, p := testFixture()
	checks, targets, _, _, err := selectedTestChecks(g, p, false)
	if err != nil {
		t.Fatal(err)
	}
	want := [][]string{
		{"pnpm", "--filter", "@example/a", "test"},
		{"pnpm", "--filter", "@example/z", "test"},
		{"uv", "run", "pytest"},
	}
	got := make([][]string, 0, len(checks))
	for _, check := range checks {
		got = append(got, check.Args)
	}
	if !reflect.DeepEqual(got, want) || !reflect.DeepEqual(targets, []string{"apps/a"}) {
		t.Fatalf("commands = %v, targets = %v", got, targets)
	}
	delete(p.Collections, "pnpm_packages")
	checks, _, _, _, err = selectedTestChecks(g, p, false)
	if err != nil || !reflect.DeepEqual(checks[0].Args, []string{"pnpm", "test:clients"}) {
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
	if err := runSelectedTests(t.TempDir(), g, p, true, false, runner, nativeRun); err != nil {
		t.Fatal(err)
	}
	if len(runner.checks) != 0 || nativeCalls != 0 {
		t.Fatalf("dry-run executed %d suite checks and %d native checks", len(runner.checks), nativeCalls)
	}
	if err := runSelectedTests(t.TempDir(), g, p, false, false, runner, nativeRun); err != nil {
		t.Fatal(err)
	}
	if len(runner.checks) != 3 || nativeCalls != 1 {
		t.Fatalf("executed %d suite checks and %d native checks", len(runner.checks), nativeCalls)
	}
}

func TestSelectedTestChecksRejectsUnknownLanguage(t *testing.T) {
	g, p := testFixture()
	suite := g.CheckGroups["python"]
	suite.Language = "unknown"
	g.CheckGroups["python"] = suite
	if _, _, _, _, err := selectedTestChecks(g, p, false); err == nil || !strings.Contains(err.Error(), `unknown language "unknown"`) {
		t.Fatalf("error = %v", err)
	}
}

func TestInvalidTestSuiteConfiguration(t *testing.T) {
	g, _ := testFixture()
	g.Jobs = []string{"python", "clients"}
	g.Collections = []collectionSpec{{Name: "pnpm_packages"}}
	tests := []execution.Suite{
		{Name: "unknown", TriggerJob: "unknown", IncludeInTest: true, Language: "python", Directory: ".", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}},
		{Name: "python", Language: "python", Directory: "../escape", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}},
		{Name: "python", Language: "python", Directory: ".", TimeoutSeconds: 0, Commands: [][]string{{"pytest"}}},
		{Name: "clients", Language: "typescript", Directory: "clients", Collection: "missing", TimeoutSeconds: 1, Commands: [][]string{{"pnpm"}}, PackageCommands: [][]string{{"pnpm", "{package}"}}},
		{Name: "python", Language: "python", Directory: ".", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}, Env: map[string]string{"BAD-KEY": "value"}},
		{Name: "both", TriggerJob: "python", IncludeInTest: true, IncludeInChecks: true, Language: "python", Directory: ".", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}},
		{Name: "skip", LocalSkip: "needs Docker", Language: "python", Directory: ".", TimeoutSeconds: 1, Commands: [][]string{{"pytest"}}},
	}
	for _, suite := range tests {
		g.CheckGroups = map[string]execution.Suite{}
		if err := g.validateCheckGroups([]execution.Suite{suite}); err == nil {
			t.Fatalf("accepted invalid suite %+v", suite)
		}
	}
}

func TestLanguagePlannerValidatesCollectionConfiguration(t *testing.T) {
	for _, tc := range []struct {
		name, language, collection, message string
		packageCommands                     [][]string
	}{
		{"unknown collection", "typescript", "missing", "unknown collection", [][]string{{"pnpm", "{package}"}}},
		{"missing package commands", "typescript", "pnpm_packages", "missing package_commands", nil},
		{"package commands without collection", "typescript", "", "require a collection", [][]string{{"pnpm", "{package}"}}},
		{"package command without placeholder", "typescript", "pnpm_packages", "requires {package}", [][]string{{"pnpm", "test"}}},
		{"go collection", "go", "pnpm_packages", "unsupported", [][]string{{"go", "test"}}},
		{"rust package commands", "rust", "", "unsupported", [][]string{{"cargo", "test"}}},
		{"python collection", "python", "pnpm_packages", "unsupported", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			g, _ := testFixture()
			g.CheckGroups = map[string]execution.Suite{}
			suite := execution.Suite{
				Name: "example", Language: tc.language, Directory: ".", TimeoutSeconds: 1,
				Collection: tc.collection, PackageCommands: tc.packageCommands,
				Commands: [][]string{{"test"}},
			}
			err := g.validateCheckGroups([]execution.Suite{suite})
			if err == nil || !strings.Contains(err.Error(), tc.message) {
				t.Fatalf("validation error = %v, want %q", err, tc.message)
			}
		})
	}
}

func TestRunCheckGroupRejectsUnknownAndInvalidCollections(t *testing.T) {
	g, _ := testFixture()
	for _, tc := range []struct {
		name, group, collection, message string
	}{
		{"unknown group", "missing", "", "unknown check group"},
		{"unexpected selection", "python", `["@example/a"]`, "does not use a collection"},
		{"malformed selection", "clients", `{"name":"@example/a"}`, "JSON array"},
		{"unregistered selection", "clients", `["@example/missing"]`, "unregistered collection value"},
		{"duplicate selection", "clients", `["@example/a","@example/a"]`, "duplicate collection value"},
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
	clients := g.CheckGroups["clients"]
	clients.AlwaysCommands = [][]string{{"pnpm", "check:ts"}}
	g.CheckGroups["clients"] = clients
	runner := &recordingRunner{}
	if err := runCheckGroup(t.TempDir(), g, "clients", `["@example/z"]`, false, runner); err != nil {
		t.Fatal(err)
	}
	want := [][]string{{"pnpm", "check:ts"}, {"pnpm", "--filter", "@example/z", "test"}}
	got := [][]string{runner.checks[0].Args, runner.checks[1].Args}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("commands = %v, want %v", got, want)
	}
}

func TestSelectedJobWithoutLocalTargetIsSkipped(t *testing.T) {
	// real_tiers has no check group included in test and no native target.
	withReal := func(p plan, jobs ...string) plan {
		for _, job := range jobs {
			p.Jobs[job] = true
		}
		return p
	}
	cases := []struct {
		name        string
		jobs        map[string]bool
		wantSkipped []string
		wantChecks  int
		wantNative  int
	}{
		{"untargeted job alone", map[string]bool{"real_tiers": true}, []string{"real_tiers"}, 0, 0},
		{"mixed with runnable jobs", map[string]bool{"real_tiers": true, "python": true, "clients": true, "evaluators": true}, []string{"real_tiers"}, 3, 1},
		{"two untargeted jobs", map[string]bool{"real_tiers": true, "slurm": true, "python": true}, []string{"real_tiers", "slurm"}, 1, 0},
		{"runnable jobs only", map[string]bool{"python": true, "clients": true, "evaluators": true}, []string{}, 3, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			g, p := testFixture()
			p.Jobs = map[string]bool{}
			withReal(p, keys(tc.jobs)...)
			if _, ok := tc.jobs["evaluators"]; !ok {
				p.Components = map[string][]string{}
			}
			runner := &recordingRunner{}
			nativeCalls := 0
			nativeRun := func(_ context.Context, _, _ string, _ []string, _ []string) error {
				nativeCalls++
				return nil
			}
			_, _, skipped, _, err := selectedTestChecks(g, p, false)
			if err != nil || !reflect.DeepEqual(skipped, tc.wantSkipped) {
				t.Fatalf("skipped = %v, err = %v, want %v", skipped, err, tc.wantSkipped)
			}
			if err := runSelectedTests(t.TempDir(), g, p, false, false, runner, nativeRun); err != nil {
				t.Fatalf("run failed: %v", err)
			}
			if len(runner.checks) != tc.wantChecks || nativeCalls != tc.wantNative {
				t.Fatalf("ran %d checks and %d native, want %d and %d", len(runner.checks), nativeCalls, tc.wantChecks, tc.wantNative)
			}
		})
	}
}

func TestSkippedJobDoesNotMaskRunnableFailure(t *testing.T) {
	g, p := testFixture()
	p.Jobs["real_tiers"] = true
	runner := &failingRunner{}
	if err := runSelectedTests(t.TempDir(), g, p, false, false, runner, nil); err == nil {
		t.Fatal("failing runnable group reported success")
	}
}

type failingRunner struct{}

func (failingRunner) Run(context.Context, string, execution.Check) error {
	return fmt.Errorf("boom")
}

func keys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func TestPolicyRejectsJobWithoutRunnableChecks(t *testing.T) {
	g, _ := testFixture()
	g.Jobs = []string{"python", "clients", "evaluators", "orphan"}
	if err := g.validateRunnableJobs(); err == nil || !strings.Contains(err.Error(), `job "orphan"`) {
		t.Fatalf("error = %v", err)
	}
	g.CheckGroups["orphan"] = execution.Suite{Name: "orphan", TriggerJob: "orphan"}
	if err := g.validateRunnableJobs(); err != nil {
		t.Fatal(err)
	}
}
