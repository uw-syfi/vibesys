package main

import (
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"repoctl/execution"
)

// localPlan returns the groups and native targets `repoctl test` would run for
// changed paths, and the groups it skips because of local_paths, using the
// repository's own policy.
func localPlan(t *testing.T, paths []string) (groups, skipped, targets []string) {
	t.Helper()
	g, err := readPolicy("../..", ".repoctl")
	if err != nil {
		t.Fatal(err)
	}
	g = g.forLocalTest()
	p, err := g.selectPaths(paths)
	if err != nil {
		t.Fatal(err)
	}
	checks, targets, _, skipped, err := selectedTestChecks(g, p)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, check := range checks {
		if !seen[check.Label] {
			seen[check.Label] = true
			groups = append(groups, check.Label)
		}
	}
	sort.Strings(groups)
	return groups, skipped, targets
}

func ciJobs(t *testing.T, paths []string) []string {
	t.Helper()
	g, err := readPolicy("../..", ".repoctl")
	if err != nil {
		t.Fatal(err)
	}
	p, err := g.selectPaths(paths)
	if err != nil {
		t.Fatal(err)
	}
	jobs := []string{}
	for job, selected := range p.Jobs {
		if selected {
			jobs = append(jobs, job)
		}
	}
	sort.Strings(jobs)
	return jobs
}

// The three example diffs from the local-time investigation. Before this
// change `repoctl test` ran every group of every selected job for each of them;
// the CI plan (the unmodified graph) must keep selecting what it did.
func TestLocalTestSelectionNarrowsHeavyGroups(t *testing.T) {
	cases := []struct {
		name        string
		paths       []string
		wantCI      []string
		wantGroups  []string
		wantSkipped []string
		wantTargets int // the number of native targets run locally; -1 skips the check
	}{
		{
			name:        "repoctl only (f39d7eb8c)",
			paths:       []string{"support/repoctl/test.go", "support/repoctl/test_test.go"},
			wantCI:      []string{"agent_image", "clients", "evaluators", "examples", "impact", "proto", "python", "real_tiers", "stitch"},
			wantGroups:  []string{"repoctl_tests"},
			wantSkipped: []string{},
			wantTargets: 0,
		},
		{
			name:        "vs-runtime outside the evaluator path (the vs-runtime part of 8b4e430df)",
			paths:       []string{"libs/vs-runtime/src/vs_runtime/_capacity_gate.py", "libs/vs-runtime/tests/test_capacity_gate.py"},
			wantCI:      []string{"python", "real_tiers"},
			wantGroups:  []string{"python_tests"},
			wantSkipped: []string{},
			wantTargets: -1,
		},
		{
			name:        "vs-runtime file the evaluator examples import (93d8548d9)",
			paths:       []string{"libs/vs-runtime/src/vs_runtime/_run_environment.py"},
			wantCI:      []string{"examples", "python", "real_tiers"},
			wantGroups:  []string{"examples_native_evaluator_tests", "examples_tests", "python_tests"},
			wantSkipped: []string{"examples_cpu_check_shard"},
			wantTargets: -1,
		},
		{
			name:        "another example (7d1ab355a)",
			paths:       []string{"examples/microservices/kvstore/README.md"},
			wantCI:      []string{"examples", "python"},
			wantGroups:  []string{"examples_native_evaluator_tests", "examples_tests", "python_tests"},
			wantSkipped: []string{"examples_cpu_check_shard"},
			wantTargets: -1,
		},
		{
			name:        "the qwen CPU-check example",
			paths:       []string{"examples/model-serving/qwen3.5-9b-mi210/.vibesys/tasks/default/cpu_check/scoring.py"},
			wantCI:      []string{"examples", "python"},
			wantGroups:  []string{"examples_cpu_check_shard", "examples_native_evaluator_tests", "examples_tests", "python_tests"},
			wantSkipped: []string{},
			wantTargets: -1,
		},
		{
			name:        "proto source selects only the contract check",
			paths:       []string{"proto/vibesys/store/v1/record.proto"},
			wantCI:      []string{"proto"},
			wantGroups:  []string{"proto"},
			wantSkipped: []string{},
			wantTargets: -1,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ciJobs(t, tc.paths); !reflect.DeepEqual(got, tc.wantCI) {
				t.Fatalf("CI jobs = %v, want %v (CI selection must not change)", got, tc.wantCI)
			}
			groups, skipped, targets := localPlan(t, tc.paths)
			if !reflect.DeepEqual(groups, tc.wantGroups) {
				t.Errorf("local groups = %v, want %v", groups, tc.wantGroups)
			}
			if !reflect.DeepEqual(skipped, tc.wantSkipped) {
				t.Errorf("locally skipped groups = %v, want %v", skipped, tc.wantSkipped)
			}
			if tc.wantTargets >= 0 && len(targets) != tc.wantTargets {
				t.Errorf("native targets = %v, want %d", targets, tc.wantTargets)
			}
		})
	}
}

func TestStitchWorkspaceChangesSelectItsChecks(t *testing.T) {
	for _, path := range []string{
		"experimental/stitch/Cargo.toml",
		"experimental/stitch/crates/stitch-core/src/lib.rs",
		"experimental/stitch/plugins/profilers/dial9/src/lib.rs",
		"experimental/stitch/plugins/source/declared-workspace/src/lib.rs",
	} {
		t.Run(path, func(t *testing.T) {
			if got, want := ciJobs(t, []string{path}), []string{"stitch"}; !reflect.DeepEqual(got, want) {
				t.Fatalf("CI jobs = %v, want %v", got, want)
			}
			groups, skipped, targets := localPlan(t, []string{path})
			if want := []string{"stitch_checks"}; !reflect.DeepEqual(groups, want) {
				t.Fatalf("local checks = %v, want %v", groups, want)
			}
			if len(skipped) != 0 || len(targets) != 0 {
				t.Fatalf("unexpected skipped checks %v or native targets %v", skipped, targets)
			}
		})
	}
}

func TestLocalJobsMustBeKnown(t *testing.T) {
	g, _ := testFixture()
	err := g.add(component{ID: "x", Roots: []string{"x"}, LocalJobs: []string{"nope"}})
	if err == nil {
		t.Fatal("unknown local job accepted")
	}
}

// The evaluator examples' file list lives in components.toml (CI selects on it) and the
// local selection reads it from there. Touching any one of its files or roots must therefore
// run the group locally, so the two cannot drift.
func TestLocalSelectionOfEvaluatorExamplesFollowsItsComponent(t *testing.T) {
	g, err := readPolicy("../..", ".repoctl")
	if err != nil {
		t.Fatal(err)
	}
	component, ok := g.Components["native_evaluator_examples"]
	if !ok {
		t.Fatal("component native_evaluator_examples is missing")
	}
	paths := append(append([]string{}, component.Files...), component.Roots...)
	if len(paths) == 0 {
		t.Fatal("component native_evaluator_examples lists no paths")
	}
	for _, path := range paths {
		groups, _, _ := localPlan(t, []string{path})
		if !contains(groups, "examples_native_evaluator_tests") {
			t.Errorf("a change to %s (listed by native_evaluator_examples) does not run examples_native_evaluator_tests locally; groups = %v", path, groups)
		}
	}
}

// Every path a group lists in local_paths must exist, so a rename cannot leave a
// local selection that silently matches nothing.
func TestLocalPathsExist(t *testing.T) {
	g, err := readPolicy("../..", ".repoctl")
	if err != nil {
		t.Fatal(err)
	}
	for name, suite := range g.CheckGroups {
		for _, path := range suite.LocalPaths {
			if _, err := os.Stat(filepath.Join("../..", path)); err != nil {
				t.Errorf("check_groups.%s: local_paths entry %q does not exist: %v", name, path, err)
			}
		}
	}
}

func TestLocalComponentsMustBeKnown(t *testing.T) {
	g, _ := testFixture()
	g.CheckGroups = map[string]execution.Suite{}
	suite := execution.Suite{
		Name: "x", Language: "python", Directory: ".", TimeoutSeconds: 1,
		Commands: [][]string{{"pytest"}}, LocalComponents: []string{"nope"},
	}
	err := g.validateCheckGroups([]execution.Suite{suite})
	if err == nil || !strings.Contains(err.Error(), "local_components") {
		t.Fatalf("validation error = %v, want a local_components rejection", err)
	}
}
