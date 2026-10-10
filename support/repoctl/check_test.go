package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func labelGroup(label string) string {
	group, _, _ := strings.Cut(label, ":")
	return group
}

func selectedLabels(t *testing.T, g graph, p plan) ([]string, []string) {
	t.Helper()
	checks, targets, err := selectedTestChecks(g, p)
	if err != nil {
		t.Fatal(err)
	}
	labels := make([]string, 0, len(checks))
	for _, check := range checks {
		labels = append(labels, check.Label)
	}
	return labels, targets
}

// Every diff-selected run is a subset of --all, and both run only local
// groups, for every assignment of the local flag to the fixture's groups.
func TestAllSelectsEveryLocalGroupAndDiffSelectionIsASubset(t *testing.T) {
	for mask := 0; mask < 4; mask++ {
		g, diff := testFixture()
		local := []string{}
		for i, name := range []string{"python", "tui"} {
			suite := g.CheckGroups[name]
			suite.RunLocal = mask&(1<<i) != 0
			g.CheckGroups[name] = suite
			if suite.RunLocal {
				local = append(local, name)
			}
		}
		for _, job := range []string{"python", "tui"} {
			if !contains(local, job) {
				delete(diff.Jobs, job)
			}
		}
		allLabels, allTargets := selectedLabels(t, g, allLocalPlan(g))
		gotGroups := []string{}
		for _, label := range allLabels {
			if group := labelGroup(label); !contains(gotGroups, group) {
				gotGroups = append(gotGroups, group)
			}
		}
		if len(gotGroups) != len(local) || len(local) > 0 && !reflect.DeepEqual(gotGroups, local) {
			t.Fatalf("mask %d: --all ran groups %v, want %v", mask, gotGroups, local)
		}
		if !reflect.DeepEqual(allTargets, []string{"apps/a"}) {
			t.Fatalf("mask %d: --all targets = %v", mask, allTargets)
		}
		diffLabels, diffTargets := selectedLabels(t, g, diff)
		for _, label := range diffLabels {
			if !contains(local, labelGroup(label)) {
				t.Fatalf("mask %d: diff ran non-local %q", mask, label)
			}
		}
		for _, target := range diffTargets {
			if !contains(allTargets, target) {
				t.Fatalf("mask %d: diff target %q missing from --all", mask, target)
			}
		}
	}
}

func TestAllRunsFullCommandsNotPackageSelection(t *testing.T) {
	g, _ := testFixture()
	checks, _, err := selectedTestChecks(g, allLocalPlan(g))
	if err != nil {
		t.Fatal(err)
	}
	got := [][]string{}
	for _, check := range checks {
		got = append(got, check.Args)
	}
	if !reflect.DeepEqual(got, [][]string{{"uv", "run", "pytest"}, {"pnpm", "test:clients"}}) {
		t.Fatalf("commands = %v", got)
	}
}

func policyWith(t *testing.T, edit func(string) string) (graph, error) {
	t.Helper()
	root := fixtureRepo(t)
	path := filepath.Join(root, "repoctl.toml")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(edit(string(data))), 0o600); err != nil {
		t.Fatal(err)
	}
	return readPolicy(root, "repoctl.toml")
}

func TestLocalKeyAndDeprecatedAliasAreEquivalent(t *testing.T) {
	alias, err := policyWith(t, func(s string) string { return s })
	if err != nil {
		t.Fatal(err)
	}
	renamed, err := policyWith(t, func(s string) string { return strings.ReplaceAll(s, "include_in_test", "local") })
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(alias.CheckGroups, renamed.CheckGroups) || !alias.CheckGroups["unit"].RunLocal {
		t.Fatalf("alias groups = %+v, renamed groups = %+v", alias.CheckGroups, renamed.CheckGroups)
	}
	off, err := policyWith(t, func(s string) string { return strings.ReplaceAll(s, "include_in_test = true", "local = false") })
	if err != nil || off.CheckGroups["unit"].RunLocal {
		t.Fatalf("local = false: err = %v, group = %+v", err, off.CheckGroups["unit"])
	}
}

func TestLocalKeyValidation(t *testing.T) {
	for _, tc := range []struct {
		name, want string
		edit       func(string) string
	}{
		{"both keys", "check_groups.unit: set local or its deprecated alias include_in_test, not both",
			func(s string) string {
				return strings.Replace(s, "include_in_test = true", "include_in_test = true\nlocal = true", 1)
			}},
		{"local without trigger job", "check_groups.unit: local requires trigger_job",
			func(s string) string {
				return strings.Replace(strings.Replace(s, "include_in_test = true", "local = true", 1), `trigger_job = "unit"`, "", 1)
			}},
		{"unknown key", "localy",
			func(s string) string { return strings.Replace(s, "include_in_test = true", "localy = true", 1) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := policyWith(t, tc.edit)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %v, want %q", err, tc.want)
			}
		})
	}
}

func TestCheckCommandAllAliasAndFlags(t *testing.T) {
	bin := buildPublicPlan(t)
	root := newPublicPlanRepo(t, testPlanConfig("a", "a", "pkg-a"))
	head := commitPublicPlan(t, root, "head")
	run := func(args ...string) (string, error) {
		full := append([]string{"--config", filepath.Join(root, "repoctl.toml")}, args...)
		cmd := exec.Command(bin, full...)
		cmd.Dir = root
		out, err := cmd.CombinedOutput()
		return string(out), err
	}
	out, err := run("check", "--all", "--dry-run")
	if err != nil || !strings.Contains(out, "a (.)") || !strings.Contains(out, "b (.)") {
		t.Fatalf("check --all: %v\n%s", err, out)
	}
	if out, err := run("check", "--all", "--base", head); err == nil || !strings.Contains(out, "--all cannot be combined with --base") {
		t.Fatalf("check --all --base: %v\n%s", err, out)
	}
	out, err = run("check", "--base", head, "--head", head, "--event", "push", "--dry-run")
	if err != nil || strings.Contains(out, "deprecated") {
		t.Fatalf("check: %v\n%s", err, out)
	}
	out, err = run("test", "--base", head, "--head", head, "--event", "push", "--dry-run")
	if err != nil || !strings.Contains(out, "deprecated alias of `check`") {
		t.Fatalf("test alias: %v\n%s", err, out)
	}
}
