package main

import (
	"os"
	"path/filepath"
	"reflect"
	"regexp"
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
	return localSelection(t, paths, false)
}

func localSelection(t *testing.T, paths []string, checksOnly bool) (groups, skipped, targets []string) {
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
	checks, targets, _, skipped, err := selectedTestChecks(g, p, checksOnly)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, check := range checks {
		name := groupName(check.Label)
		if !seen[name] {
			seen[name] = true
			groups = append(groups, name)
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
			wantCI:      []string{"agent_image", "clients", "evaluators", "examples", "gateway", "impact", "proto", "python", "real_tiers", "stitch"},
			wantGroups:  []string{"always_on", "repoctl_tests"},
			wantSkipped: []string{},
			wantTargets: 0,
		},
		{
			name:        "vs-runtime outside the evaluator path (the vs-runtime part of 8b4e430df)",
			paths:       []string{"libs/vs-runtime/src/vs_runtime/_capacity_gate.py", "libs/vs-runtime/tests/test_capacity_gate.py"},
			wantCI:      []string{"python", "real_tiers"},
			wantGroups:  []string{"always_on", "python_architecture", "python_guards", "python_quality", "python_tests", "python_types"},
			wantSkipped: []string{"minimal_container_tests", "slurm_cluster_tests"},
			wantTargets: -1,
		},
		{
			name:        "vs-runtime file the evaluator examples import (93d8548d9)",
			paths:       []string{"libs/vs-runtime/src/vs_runtime/_run_environment.py"},
			wantCI:      []string{"examples", "python", "real_tiers"},
			wantGroups:  []string{"always_on", "examples_native_evaluator_tests", "examples_tests", "python_architecture", "python_guards", "python_quality", "python_tests", "python_types"},
			wantSkipped: []string{"examples_cpu_check_shard", "minimal_container_tests", "slurm_cluster_tests"},
			wantTargets: -1,
		},
		{
			name:        "another example (7d1ab355a)",
			paths:       []string{"examples/microservices/kvstore/README.md"},
			wantCI:      []string{"examples", "python"},
			wantGroups:  []string{"always_on", "examples_native_evaluator_tests", "examples_tests", "python_architecture", "python_guards", "python_quality", "python_tests", "python_types"},
			wantSkipped: []string{"examples_cpu_check_shard"},
			wantTargets: -1,
		},
		{
			name:        "the qwen CPU-check example",
			paths:       []string{"examples/model-serving/qwen3.5-9b-mi210/.vibesys/tasks/default/cpu_check/scoring.py"},
			wantCI:      []string{"examples", "python"},
			wantGroups:  []string{"always_on", "examples_cpu_check_shard", "examples_native_evaluator_tests", "examples_tests", "python_architecture", "python_guards", "python_quality", "python_tests", "python_types"},
			wantSkipped: []string{},
			wantTargets: -1,
		},
		{
			name:        "gateway source selects only the Go checks",
			paths:       []string{"gateway/internal/server/host.go"},
			wantCI:      []string{"gateway"},
			wantGroups:  []string{"always_on", "gateway_checks"},
			wantSkipped: []string{},
			wantTargets: 0,
		},
		{
			name:        "generated Go bindings select contract and gateway checks",
			paths:       []string{"gateway/gen/vibesys/api/v1/host.pb.go"},
			wantCI:      []string{"gateway", "proto"},
			wantGroups:  []string{"always_on", "gateway_checks", "proto"},
			wantSkipped: []string{},
			wantTargets: 0,
		},
		{
			name:        "proto source selects the contract check and the client checks that read its replays",
			paths:       []string{"proto/vibesys/store/v1/record.proto"},
			wantCI:      []string{"clients", "proto"},
			wantGroups:  []string{"always_on", "clients_build", "clients_quality", "clients_tests", "proto"},
			wantSkipped: []string{"clients_e2e"},
			wantTargets: -1,
		},
		{
			name:        "python only selects the python quality groups",
			paths:       []string{"libs/vs-dst/src/vs_dst/__init__.py"},
			wantCI:      []string{"python"},
			wantGroups:  []string{"always_on", "python_architecture", "python_guards", "python_quality", "python_tests", "python_types"},
			wantSkipped: []string{},
			wantTargets: 0,
		},
		{
			name:        "clients change selects the client quality groups",
			paths:       []string{"clients/features/run-intents/src/state/machine.ts"},
			wantCI:      []string{"clients"},
			wantGroups:  []string{"always_on", "clients_build", "clients_quality", "clients_tests"},
			wantSkipped: []string{"clients_e2e"},
			wantTargets: 0,
		},
		{
			name:        "docs only selects the always-on scripts",
			paths:       []string{"docs/contributing/development.md"},
			wantCI:      []string{"examples"},
			wantGroups:  []string{"always_on"},
			wantSkipped: []string{},
			wantTargets: 0,
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
			if want := []string{"always_on", "stitch_checks"}; !reflect.DeepEqual(groups, want) {
				t.Fatalf("local checks = %v, want %v", groups, want)
			}
			if len(skipped) != 0 || len(targets) != 0 {
				t.Fatalf("unexpected skipped checks %v or native targets %v", skipped, targets)
			}
		})
	}
}

func TestEveryProtoPathSelectsChecksOnlyVerification(t *testing.T) {
	for _, path := range []string{
		"proto/vibesys/api/v1/host.proto",
		"proto/vibesys/store/v1/record.proto",
		"proto/buf/validate/validate.proto",
		"proto/new_package/deeply/nested/contract.proto",
		"proto/new.proto",
	} {
		t.Run(path, func(t *testing.T) {
			groups, skipped, targets := localSelection(t, []string{path}, true)
			want, wantSkipped := []string{"always_on", "proto"}, []string{}
			// A schema under proto/vibesys also keys the native replays the client tests read.
			if strings.HasPrefix(path, "proto/vibesys/") {
				want = []string{"always_on", "clients_build", "clients_quality", "proto"}
				wantSkipped = []string{"clients_e2e"}
			}
			if !reflect.DeepEqual(groups, want) {
				t.Fatalf("checks-only groups = %v, want %v", groups, want)
			}
			if !reflect.DeepEqual(skipped, wantSkipped) || len(targets) != 0 {
				t.Fatalf("skipped groups = %v, want %v, or native targets %v", skipped, wantSkipped, targets)
			}
		})
	}
}

func TestChecksOnlyExcludesTestGroups(t *testing.T) {
	cases := []struct {
		name  string
		paths []string
		want  []string
	}{
		{"python", []string{"libs/vs-dst/src/vs_dst/__init__.py"}, []string{"always_on", "python_architecture", "python_guards", "python_quality", "python_types"}},
		{"clients", []string{"clients/features/run-intents/src/state/machine.ts"}, []string{"always_on", "clients_build", "clients_quality"}},
		{"proto", []string{"proto/vibesys/store/v1/record.proto"}, []string{"always_on", "clients_build", "clients_quality", "proto"}},
		{"docs", []string{"docs/contributing/development.md"}, []string{"always_on"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			groups, skipped, targets := localSelection(t, tc.paths, true)
			if !reflect.DeepEqual(groups, tc.want) {
				t.Errorf("checks-only groups = %v, want %v", groups, tc.want)
			}
			if len(targets) != 0 {
				t.Errorf("checks-only ran native targets %v", targets)
			}
			for _, name := range groups {
				if name == "python_tests" || name == "clients_tests" || name == "repoctl_tests" {
					t.Errorf("checks-only included test group %s", name)
				}
			}
			if tc.name == "clients" && !reflect.DeepEqual(skipped, []string{"clients_e2e"}) {
				t.Errorf("skipped = %v, want [clients_e2e]", skipped)
			}
		})
	}
}

// CI-only groups that are the sharded or artifact form of a local run, or that
// need a host `repoctl test` does not provide. A group added to the workflow
// must be include_in_test, include_in_checks, or local_skip, not a new entry here.
var ciFormGroups = map[string]string{
	"python_ci_tests_shard":        "local group python_tests runs the suite; CI shards it, and the job is python or clients, which one trigger_job cannot express",
	"python_impact_tests":          "CI's commit-stage form of python_tests (and the pre-push gate's, through gate_commands); `repoctl test` already runs the whole suite as python_tests",
	"python_ci_coverage":           "needs the shards' coverage data",
	"patch_coverage":               "needs coverage.xml from the shards",
	"sandbox_tests":                "needs bubblewrap on Linux; the group is opt-in",
	"microservice_workload_checks": "CI runs it beside examples_tests; it is a workload check, not a Docker or real-tier group",
}

func TestWorkflowCheckGroupsHaveALocalDisposition(t *testing.T) {
	workflow, err := os.ReadFile("../../.github/workflows/test.yml")
	if err != nil {
		t.Fatal(err)
	}
	g, err := readPolicy("../..", ".repoctl")
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, match := range regexp.MustCompile(`run-checks --group ([A-Za-z0-9_]+)`).FindAllStringSubmatch(string(workflow), -1) {
		name := match[1]
		if seen[name] {
			continue
		}
		seen[name] = true
		suite, ok := g.CheckGroups[name]
		if !ok {
			t.Errorf("workflow runs unknown check group %s", name)
			continue
		}
		classified := suite.IncludeInTest || suite.IncludeInChecks || suite.LocalSkip != ""
		if _, form := ciFormGroups[name]; form {
			if classified {
				t.Errorf("check group %s is listed as a CI form and also has a local disposition; keep one", name)
			}
			continue
		}
		if !classified {
			t.Errorf("workflow group %s has no local disposition: set include_in_test, include_in_checks, or local_skip", name)
		}
	}
	for name := range ciFormGroups {
		if !seen[name] {
			t.Errorf("CI form group %s is no longer in the workflow", name)
		}
	}
}

func TestAlwaysOnCommandsAreWhatCIRunsOnEveryChange(t *testing.T) {
	g, err := readPolicy("../..", ".repoctl")
	if err != nil {
		t.Fatal(err)
	}
	suite, ok := g.CheckGroups["always_on"]
	if !ok || !suite.IncludeInChecks || suite.TriggerJob != "" {
		t.Fatalf("always_on = %+v, want include_in_checks with no trigger job", suite)
	}
	workflow, err := os.ReadFile("../../.github/workflows/test.yml")
	if err != nil {
		t.Fatal(err)
	}
	text := string(workflow)
	want := []string{
		"validate",
		"verify-policy",
		"scripts/check_stale_revert.py",
		"scripts/check_stray_files.py",
		"scripts/check_doc_links.py",
		"scripts.check_doc_citations",
		"scripts.check_skills",
	}
	got := []string{}
	for _, command := range suite.Commands {
		token := alwaysOnToken(command)
		got = append(got, token)
		if !strings.Contains(text, token) {
			t.Errorf("always_on token %q is not in the test workflow", token)
		}
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("always_on tokens = %v, want %v", got, want)
	}
}

func alwaysOnToken(command []string) string {
	if len(command) > 1 && command[0] == "./support/repoctl/repoctl" {
		return command[1]
	}
	for _, arg := range command {
		if strings.Contains(arg, "scripts") {
			return arg
		}
	}
	return command[len(command)-1]
}

func TestLocalJobsMustBeKnown(t *testing.T) {
	g, _ := testFixture()
	err := g.add(component{ID: "x", Roots: []string{"x"}, LocalJobs: []string{"nope"}})
	if err == nil {
		t.Fatal("unknown local job accepted")
	}
}

func TestLocalIgnoreRootsMustBeComponentRoots(t *testing.T) {
	g, _ := testFixture()
	err := g.add(component{ID: "x", Roots: []string{"x"}, LocalIgnoreRoots: []string{"y"}})
	if err == nil || !strings.Contains(err.Error(), "local_ignore_roots") {
		t.Fatalf("error = %v, want a local_ignore_roots rejection", err)
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
