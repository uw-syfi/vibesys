package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"repoctl/execution"
)

// Exercise the public command with a Runner fake: Git supplies actual changed
// paths, while no Python tests or external coverage commands execute here.
func TestCoverageOnlyCommand(t *testing.T) {
	for _, tc := range []struct {
		name         string
		extra        []string
		dry          bool
		failAt       int
		wantErr      string
		deleteSource bool
		sourceRoot   string
	}{
		{name: "runs only own tests"},
		{name: "library source", sourceRoot: "libs/tool/src/tool"},
		{name: "SDK source", sourceRoot: "sdk/tool/src/tool"},
		{name: "dry run", dry: true},
		{name: "deleted source needs no measurement", deleteSource: true, wantErr: "no touched Python source files"},
		{name: "real tier excluded", extra: []string{"--coverage-test", "tests/e2e/test_x.py"}, wantErr: "real-tier"},
		{name: "missing tests", extra: []string{}, wantErr: "supply the touched modules' own test files"},
		{name: "reject directory", extra: []string{"--coverage-test", "a"}, wantErr: "test_*.py"},
		{name: "reject traversal", extra: []string{"--coverage-test", "../test_x.py"}, wantErr: "repository-relative"},
		{name: "missing test file", extra: []string{"--coverage-test", "a/test_missing.py"}, wantErr: "test_missing.py"},
		{name: "conflicting mode", extra: []string{"--checks-only"}, wantErr: "cannot combine"},
		{name: "test failure stops reporting", failAt: 1, wantErr: "fake failure"},
		{name: "report failure stops floor", failAt: 2, wantErr: "fake failure"},
		{name: "xml failure stops gates", failAt: 3, wantErr: "fake failure"},
		{name: "floor failure propagates", failAt: 4, wantErr: "fake failure"},
		{name: "patch failure propagates", failAt: 5, wantErr: "fake failure"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sourceRoot := tc.sourceRoot
			if sourceRoot == "" {
				sourceRoot = "src"
			}
			root := newPublicPlanRepo(t, testPlanConfig("a", sourceRoot, "pkg-a")+coverageGateConfig)
			if err := os.MkdirAll(filepath.Join(root, sourceRoot), 0755); err != nil {
				t.Fatal(err)
			}
			source := filepath.Join(root, sourceRoot, "alpha.py")
			if err := os.WriteFile(source, []byte("value = 1\n"), 0644); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(root, "a/test_alpha.py"), []byte("def test_alpha(): pass\n"), 0644); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(root, sourceRoot, "deleted.py"), []byte("value = 1\n"), 0644); err != nil {
				t.Fatal(err)
			}
			base := commitPublicPlan(t, root, "base")
			if err := os.Remove(filepath.Join(root, sourceRoot, "deleted.py")); err != nil {
				t.Fatal(err)
			}
			if err := os.Mkdir(filepath.Join(root, sourceRoot, "tests"), 0755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(root, sourceRoot, "tests/test_changed.py"), []byte("def test_changed(): pass\n"), 0644); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(root, sourceRoot, "conftest.py"), []byte("value = 1\n"), 0644); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(source, []byte("value = 2\n"), 0644); err != nil {
				t.Fatal(err)
			}
			if tc.deleteSource {
				if err := os.Remove(source); err != nil {
					t.Fatal(err)
				}
			}
			args := []string{"--config", filepath.Join(root, "repoctl.toml"), "test", "--base", base, "--coverage-only"}
			if tc.extra == nil {
				args = append(args, "--coverage-test", "a/test_alpha.py")
			} else {
				args = append(args, tc.extra...)
			}
			if tc.dry {
				args = append(args, "--dry-run")
			}
			runner := &coverageRunner{failAt: tc.failAt}
			err := cliWithRunner(args, runner)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("error = %v, want %s", err, tc.wantErr)
				}
				if len(runner.checks) != tc.failAt {
					t.Fatalf("ran %d commands, want %d", len(runner.checks), tc.failAt)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if tc.dry {
				if len(runner.checks) != 0 {
					t.Fatal("dry run executed commands")
				}
				return
			}
			if len(runner.checks) != 5 {
				t.Fatalf("checks = %v", runner.checks)
			}
			pytest := strings.Join(runner.checks[0].Args, " ")
			if !strings.Contains(pytest, "--cov="+sourceRoot) || !strings.Contains(pytest, "-n 0") || !strings.HasSuffix(pytest, "a/test_alpha.py") || strings.Contains(pytest, "--no-cov") {
				t.Fatalf("pytest = %s", pytest)
			}
			if !strings.Contains(strings.Join(runner.checks[1].Args, " "), "--include="+sourceRoot+"/alpha.py") {
				t.Fatal("report does not select touched module")
			}
			floor := strings.Join(runner.checks[3].Args, " ")
			if !strings.Contains(floor, "scripts/check_coverage_floor.py") || !strings.HasSuffix(floor, "--module "+sourceRoot+"/alpha.py") {
				t.Fatalf("floor = %s", floor)
			}
			xml := strings.Join(runner.checks[2].Args, " ")
			if !strings.Contains(xml, "coverage xml") || !strings.Contains(xml, "--include="+sourceRoot+"/alpha.py") {
				t.Fatalf("xml = %s", xml)
			}
			patch := strings.Join(runner.checks[4].Args, " ")
			if patch != "patch-tool artifacts/repoctl-coverage/report.xml --fail-under=91 --compare-branch=policy-base" {
				t.Fatalf("patch command did not preserve configured policy: %s", patch)
			}
			for _, check := range runner.checks[:3] {
				if check.Env["COVERAGE_FILE"] != "artifacts/repoctl-coverage/data" {
					t.Fatal("coverage data is not isolated")
				}
			}
		})
	}
}

type coverageRunner struct {
	checks []execution.Check
	failAt int
}

func (r *coverageRunner) Run(_ context.Context, _ string, check execution.Check) error {
	r.checks = append(r.checks, check)
	if len(r.checks) == r.failAt {
		return fmt.Errorf("fake failure")
	}
	return nil
}

// Distinct patch settings prove the command uses policy, rather than a copied
// CI threshold or branch in the implementation.
const coverageGateConfig = `
[[check_groups]]
name = "python_coverage_floor"
language = "python"
directory = "."
timeout_seconds = 60
commands = [["python", "scripts/check_coverage_floor.py", "coverage.json"]]

[[check_groups]]
name = "patch_coverage"
language = "python"
directory = "."
timeout_seconds = 60
commands = [["patch-tool", "coverage.xml", "--fail-under=91", "--compare-branch=policy-base"]]
`

func TestCombinedCICoverageInvokesTheSharedFloorGroup(t *testing.T) {
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	runner := &coverageRunner{}
	if err := cliWithRunner([]string{"--config", filepath.Join(root, ".repoctl"), "run-checks", "--group", "python_ci_coverage"}, runner); err != nil {
		t.Fatal(err)
	}
	last := runner.checks[len(runner.checks)-1]
	if strings.Join(last.Args, " ") != "./support/repoctl/repoctl run-checks --group python_coverage_floor" {
		t.Fatalf("CI does not invoke the shared floor policy: %v", last.Args)
	}
}
