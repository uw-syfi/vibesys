package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

type publicPlan struct {
	Jobs         map[string]bool     `json:"jobs"`
	ChangedPaths []string            `json:"changed_paths"`
	Collections  map[string][]string `json:"collections"`
}

// These tests exercise repoctl as a user does. They assert the plan contract,
// without depending on the graph or selection implementation.
func TestPlanUsesOwnershipAtBothRevisions(t *testing.T) {
	tests := []struct {
		name         string
		baseConfig   string
		headConfig   string
		mutate       func(string) error
		wantJobs     []string
		wantPaths    []string
		wantPackages []string
	}{
		{
			name:       "added path uses head ownership",
			baseConfig: testPlanConfig("a", "a", "pkg-a"),
			headConfig: testPlanConfig("a", "a", "pkg-a"),
			mutate:     func(root string) error { return os.WriteFile(filepath.Join(root, "a/new.txt"), []byte("new\n"), 0o644) },
			wantJobs:   []string{"build-a"}, wantPaths: []string{"a/new.txt"}, wantPackages: []string{"pkg-a"},
		},
		{
			name:       "deleted path uses base ownership",
			baseConfig: testPlanConfig("a", "a", "pkg-a"),
			headConfig: testPlanConfig("a", "a", "pkg-a"),
			mutate:     func(root string) error { return os.Remove(filepath.Join(root, "a/old.txt")) },
			wantJobs:   []string{"build-a"}, wantPaths: []string{"a/old.txt"}, wantPackages: []string{"pkg-a"},
		},
		{
			name:       "modified path uses both historical and current owners",
			baseConfig: testPlanConfig("a", "a", "pkg-a"),
			headConfig: testPlanConfig("b", "a", "pkg-b"),
			mutate: func(root string) error {
				return os.WriteFile(filepath.Join(root, "a/old.txt"), []byte("changed\n"), 0o644)
			},
			wantJobs: []string{"build-a", "build-b"}, wantPaths: []string{"a/old.txt", "repoctl.toml"}, wantPackages: []string{"pkg-b"},
		},
		{
			name:       "path moved between owners selects both jobs and current package",
			baseConfig: testPlanConfig("a", "a", "pkg-a"),
			headConfig: testPlanConfig("b", "b", "pkg-b"),
			mutate: func(root string) error {
				if err := os.Remove(filepath.Join(root, "a/old.txt")); err != nil {
					return err
				}
				return os.WriteFile(filepath.Join(root, "b/new.txt"), []byte("new\n"), 0o644)
			},
			wantJobs: []string{"build-a", "build-b"}, wantPaths: []string{"a/old.txt", "b/new.txt", "repoctl.toml"}, wantPackages: []string{"pkg-b"},
		},
		{
			name:       "renamed path selects old and new owners",
			baseConfig: testPlanConfig("a", "a", "pkg-a"),
			headConfig: testPlanConfig("b", "b", "pkg-b"),
			mutate: func(root string) error {
				return os.Rename(filepath.Join(root, "a/old.txt"), filepath.Join(root, "b/new.txt"))
			},
			wantJobs: []string{"build-a", "build-b"}, wantPaths: []string{"a/old.txt", "b/new.txt", "repoctl.toml"}, wantPackages: []string{"pkg-b"},
		},
	}

	bin := buildPublicPlan(t)
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			root := newPublicPlanRepo(t, tt.baseConfig)
			base := commitPublicPlan(t, root, "base")
			if err := tt.mutate(root); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(root, "repoctl.toml"), []byte(tt.headConfig), 0o644); err != nil {
				t.Fatal(err)
			}
			head := commitPublicPlan(t, root, "head")
			got := runPublicPlan(t, bin, root, base, head)
			if selected := selectedPublicJobs(got.Jobs); !reflect.DeepEqual(selected, tt.wantJobs) {
				t.Errorf("selected jobs = %v, want %v", selected, tt.wantJobs)
			}
			sortStrings(got.ChangedPaths)
			if !reflect.DeepEqual(got.ChangedPaths, tt.wantPaths) {
				t.Errorf("changed paths = %v, want %v", got.ChangedPaths, tt.wantPaths)
			}
			if got := got.Collections["packages"]; !reflect.DeepEqual(got, tt.wantPackages) {
				t.Errorf("packages = %v, want %v", got, tt.wantPackages)
			}
		})
	}
}

func TestPlanRejectsUnownedAddedPath(t *testing.T) {
	bin := buildPublicPlan(t)
	root := newPublicPlanRepo(t, testPlanConfig("a", "a", "pkg-a"))
	base := commitPublicPlan(t, root, "base")
	if err := os.WriteFile(filepath.Join(root, "outside.txt"), []byte("unowned\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	head := commitPublicPlan(t, root, "head")
	cmd := exec.Command(bin, "--config", filepath.Join(root, "repoctl.toml"), "plan", "--base", base, "--head", head, "--event", "push", "--json")
	cmd.Dir = root
	out, err := cmd.CombinedOutput()
	if err == nil || !strings.Contains(string(out), "unowned changed paths: outside.txt") {
		t.Fatalf("repoctl plan output = %q, error = %v; want an unowned-path failure", out, err)
	}
}

func TestPullRequestPlanIgnoresChangesAfterMergeBase(t *testing.T) {
	bin := buildPublicPlan(t)
	root := newPublicPlanRepo(t, testPlanConfig("a", "a", "pkg-a"))
	base := commitPublicPlan(t, root, "base")
	gitPublicPlan(t, root, "checkout", "-qb", "feature")
	if err := os.WriteFile(filepath.Join(root, "a/old.txt"), []byte("feature change\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	featureHead := commitPublicPlan(t, root, "feature change")
	gitPublicPlan(t, root, "checkout", "-qb", "target", base)
	if err := os.WriteFile(filepath.Join(root, "b/other.txt"), []byte("target change\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	targetHead := commitPublicPlan(t, root, "target change")
	gitPublicPlan(t, root, "checkout", "feature")
	got := runPublicPlanEvent(t, bin, root, targetHead, featureHead, "pull_request")
	if selected := selectedPublicJobs(got.Jobs); !reflect.DeepEqual(selected, []string{"build-a"}) {
		t.Fatalf("selected jobs = %v, want [build-a]", selected)
	}
	if !reflect.DeepEqual(got.ChangedPaths, []string{"a/old.txt"}) {
		t.Fatalf("changed paths = %v, want [a/old.txt]", got.ChangedPaths)
	}
}

func TestTestCommandUsesCommittedAndWorkingTreeOwnership(t *testing.T) {
	bin := buildPublicPlan(t)
	root := newPublicPlanRepo(t, testPlanConfig("a", "a", "pkg-a"))
	head := commitPublicPlan(t, root, "head")
	if err := os.WriteFile(filepath.Join(root, "repoctl.toml"), []byte(testPlanConfig("b", "a", "pkg-b")), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "a/old.txt"), []byte("working tree change\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(bin, "--config", filepath.Join(root, "repoctl.toml"), "test", "--base", head, "--head", head, "--event", "push", "--dry-run")
	cmd.Dir = root
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("repoctl test: %v\n%s", err, out)
	}
	if !strings.Contains(string(out), "build-a") || !strings.Contains(string(out), "build-b") {
		t.Fatalf("repoctl test output = %q, want historical and current owners", out)
	}
}

func testPlanConfig(id, root, name string) string {
	job := "build-" + id
	return `default_base = "HEAD~1"
jobs = ["build-a", "build-b"]
ignored_files = ["repoctl.toml"]
ignored_roots = []
discoveries = []
edges = []

[[collections]]
name = "packages"
class = "package"
field = "name"

[[check_groups]]
name = "a"
trigger_job = "build-a"
include_in_test = true
language = "go"
directory = "."
timeout_seconds = 1
commands = [["true"]]

[[check_groups]]
name = "b"
trigger_job = "build-b"
include_in_test = true
language = "go"
directory = "."
timeout_seconds = 1
commands = [["true"]]

[[components]]
id = "` + id + `"
class = "package"
name = "` + name + `"
root = "` + root + `"
jobs = ["` + job + `"]
`
}

func testPlanConfigWithoutComponents() string {
	config := testPlanConfig("a", "a", "pkg-a")
	return strings.Split(config, "[[components]]")[0] + "components = []\n"
}

func newPublicPlanRepo(t *testing.T, config string) string {
	t.Helper()
	root := t.TempDir()
	for _, dir := range []string{"a", "b"} {
		if err := os.Mkdir(filepath.Join(root, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for path, body := range map[string]string{
		"repoctl.toml": config,
		"a/old.txt":    "old\n",
		"b/other.txt":  "other\n",
	} {
		if err := os.WriteFile(filepath.Join(root, path), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	gitPublicPlan(t, root, "init", "-q")
	gitPublicPlan(t, root, "config", "user.email", "repoctl-test@example.invalid")
	gitPublicPlan(t, root, "config", "user.name", "repoctl test")
	return root
}

func commitPublicPlan(t *testing.T, root, message string) string {
	t.Helper()
	gitPublicPlan(t, root, "add", "-A")
	gitPublicPlan(t, root, "commit", "-qm", message)
	return strings.TrimSpace(gitPublicPlan(t, root, "rev-parse", "HEAD"))
}

func gitPublicPlan(t *testing.T, root string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = root
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, out)
	}
	return string(out)
}

func runPublicPlan(t *testing.T, bin, root, base, head string) publicPlan {
	return runPublicPlanEvent(t, bin, root, base, head, "push")
}

func runPublicPlanEvent(t *testing.T, bin, root, base, head, event string) publicPlan {
	t.Helper()
	cmd := exec.Command(bin, "--config", filepath.Join(root, "repoctl.toml"), "plan", "--base", base, "--head", head, "--event", event, "--json")
	cmd.Dir = root
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("repoctl plan: %v\n%s", err, out)
	}
	var result publicPlan
	if err := json.Unmarshal(out, &result); err != nil {
		t.Fatalf("decode repoctl plan: %v\n%s", err, out)
	}
	return result
}

func buildPublicPlan(t *testing.T) string {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "repoctl")
	cmd := exec.Command("go", "build", "-o", bin, ".")
	cmd.Dir = "."
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("build repoctl: %v\n%s", err, out)
	}
	return bin
}

func selectedPublicJobs(jobs map[string]bool) []string {
	selected := []string{}
	for name, enabled := range jobs {
		if enabled {
			selected = append(selected, name)
		}
	}
	sortStrings(selected)
	return selected
}

func sortStrings(items []string) {
	for i := 0; i < len(items); i++ {
		for j := i + 1; j < len(items); j++ {
			if items[j] < items[i] {
				items[i], items[j] = items[j], items[i]
			}
		}
	}
}
