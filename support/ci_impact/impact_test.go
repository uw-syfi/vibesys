package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func fixtureRepo(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	files := map[string]string{
		"ci-impact.toml": `default_base = "trunk"
jobs = ["unit", "web", "native", "policy"]
ignored_roots = ["external"]
ignored_files = ["LICENSE"]

[[discoveries]]
adapter = "tach"
config = "modules.toml"
class = "python_module"
id_prefix = "py:"
job = "unit"
ownership_group = "python"

[[discoveries]]
adapter = "package_json"
manifest_glob = "packages/*/package.json"
class = "package"
id_prefix = "pkg:"
job = "web"
ownership_group = "packages"
workspace_prefix = "workspace:"

[[discoveries]]
adapter = "manifest_directories"
class = "native"
id_prefix = "target:"
target = true
ownership_group = "native"
manifests = { "go.mod" = "go", "Cargo.toml" = "rust" }
scope_roots = ["native"]
selected_roots = ["native/app", "native/engine", "native/engine/child"]
selected_jobs = ["native"]

[[collections]]
name = "native_targets"
class = "native"
field = "root"
selected_only = true

[[collections]]
name = "native_languages"
class = "native"
field = "language"
selected_only = true

[[collections]]
name = "packages"
class = "package"
field = "name"

[native_checks]
timeout_seconds = 90
[native_checks.commands]
go = [["go", "test", "./..."]]
rust = [["cargo", "test"]]

[[components]]
id = "protocol"
class = "manual"
files = ["shared/protocol.txt"]

[[components]]
id = "policy"
files = ["ci-impact.toml"]
jobs = ["unit", "web", "native", "policy"]
select_all_jobs = true
select_all_collections = true

[[edges]]
from = "py:base"
to = "py:service"

[[edges]]
from = "pkg:@test/base"
to = "pkg:@test/app"

[[edges]]
from = "target:native/engine/child"
to = "target:native/engine"
`,
		"modules.toml": `source_roots = ["python"]
[[modules]]
path = "base"
[[modules]]
path = "service"
depends_on = ["base"]
`,
		"python/base/__init__.py":    "",
		"python/service/__init__.py": "",
		"packages/base/package.json": `{"name":"@test/base"}`,
		"packages/app/package.json":  `{"name":"@test/app","dependencies":{"@test/base":"workspace:*"}}`,
		"native/app/go.mod":          "module example.test/app\n",
		"native/engine/Cargo.toml":   "[package]\nname = \"engine\"\nversion = \"0.1.0\"\n",
		"native/engine/child/go.mod": "module example.test/child\n",
		"shared/protocol.txt":        "protocol\n",
		"external/unowned.txt":       "ignored\n",
		"LICENSE":                    "license\n",
	}
	for name, contents := range files {
		path := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(contents), 0600); err != nil {
			t.Fatal(err)
		}
	}
	gitTest(t, root, "init", "-q")
	return root
}

func gitTest(t *testing.T, root string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = root
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v: %s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func initGitRepo(t *testing.T, root string) {
	t.Helper()
	gitTest(t, root, "init", "-q")
	gitTest(t, root, "config", "user.email", "ci@example.invalid")
	gitTest(t, root, "config", "user.name", "CI Test")
}

func writeFixtureFile(t *testing.T, root, name, contents string) {
	t.Helper()
	path := filepath.Join(root, filepath.FromSlash(name))
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(contents), 0600); err != nil {
		t.Fatal(err)
	}
}

func commitFixture(t *testing.T, root, message string) string {
	t.Helper()
	gitTest(t, root, "add", "-A")
	gitTest(t, root, "commit", "-qm", message)
	return gitTest(t, root, "rev-parse", "HEAD")
}

func TestConfiguredDiscoveriesAndEffects(t *testing.T) {
	g, err := readPolicy(fixtureRepo(t), "ci-impact.toml")
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		path       string
		job        string
		collection string
		want       []string
	}{
		{"python/service/handler.py", "unit", "", nil},
		{"packages/base/src/index.ts", "web", "packages", []string{"@test/app", "@test/base"}},
		{"native/engine/child/go.mod", "native", "native_targets", []string{"native/engine", "native/engine/child"}},
		{"native/app/main.go", "native", "native_languages", []string{"go"}},
		{"shared/protocol.txt", "", "", nil},
	}
	for _, tt := range tests {
		t.Run(tt.path, func(t *testing.T) {
			p, err := g.selectPaths([]string{tt.path})
			if err != nil {
				t.Fatal(err)
			}
			if tt.job != "" && !p.Jobs[tt.job] {
				t.Errorf("expected job %q: %#v", tt.job, p.Jobs)
			}
			if tt.collection != "" && !reflect.DeepEqual(p.Collections[tt.collection], tt.want) {
				t.Errorf("collection %s = %v, want %v", tt.collection, p.Collections[tt.collection], tt.want)
			}
		})
	}
	p, err := g.selectPaths([]string{"not/owned.txt"})
	if err == nil || !strings.Contains(err.Error(), "unowned") {
		t.Fatalf("unknown path: %v, %v", p, err)
	}
	p, err = g.selectPaths([]string{"external/unowned.txt", "LICENSE"})
	if err != nil || len(p.Jobs) != 4 {
		t.Fatalf("ignored paths: %v, %v", p, err)
	}
}

func TestGraphClosureAndRootOwnership(t *testing.T) {
	g := graph{Components: map[string]component{}, Jobs: []string{"unit"}}
	components := []component{
		{ID: "rootless", Class: "python_module", OwnershipGroup: "modules", Files: []string{"loose.py"}},
		{ID: "parent", Roots: []string{"pkg", "pkg/deep"}, OwnershipGroup: "modules"},
		{ID: "child", Roots: []string{"pkg/deep/child"}, OwnershipGroup: "modules", DependsOn: []string{"parent"}, Jobs: []string{"unit"}},
	}
	for _, c := range components {
		if err := g.add(c); err != nil {
			t.Fatal(err)
		}
	}
	if got := g.owners("pkg/deep/child/file.go"); !reflect.DeepEqual(got, []string{"child"}) {
		t.Fatalf("deepest owner = %v", got)
	}
	if got := g.owners("loose.py"); !reflect.DeepEqual(got, []string{"rootless"}) {
		t.Fatalf("rootless file owner = %v", got)
	}
	p, err := g.selectPaths([]string{"pkg/file.go"})
	if err != nil || !p.Jobs["unit"] {
		t.Fatalf("reverse dependency closure = %v, %v", p, err)
	}
}

func TestNativeDiscoveryRejectsTwoManifestsInOneDirectory(t *testing.T) {
	root := fixtureRepo(t)
	if err := os.WriteFile(filepath.Join(root, "native/app/Cargo.toml"), []byte("[package]\nname=\"app\"\nversion=\"0.1.0\"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := readPolicy(root, "ci-impact.toml"); err == nil || !strings.Contains(err.Error(), "multiple configured manifests") {
		t.Fatalf("dual manifest error = %v", err)
	}
}

func TestPathValidationAndCollectionSelection(t *testing.T) {
	for _, path := range []string{"", "/abs", "../escape", "a/../b", "a//b"} {
		if validPath(path) {
			t.Errorf("validPath(%q) = true", path)
		}
	}
	g := graph{Components: map[string]component{}, Jobs: []string{"all"}, Collections: []collectionSpec{{Name: "targets", Class: "target", Field: "root"}}}
	if err := g.add(component{ID: "selection", Class: "target", Roots: []string{"src"}, Selected: true, SelectAllJobs: true}); err != nil {
		t.Fatal(err)
	}
	p, err := g.selectPaths([]string{"src/file"})
	if err != nil || !p.Jobs["all"] || !reflect.DeepEqual(p.Collections["targets"], []string{"src"}) {
		t.Fatalf("configured broad selection = %#v, %v", p, err)
	}
	if len(p.JobReasons["all"]) == 0 {
		t.Fatalf("broad selection has no explanation: %#v", p.JobReasons)
	}
}

func TestAbsoluteConfigLocatesItsRepository(t *testing.T) {
	root := fixtureRepo(t)
	t.Setenv("CI_IMPACT_ROOT", filepath.Join(root, "wrong-root"))
	configPath := filepath.Join(root, "ci-impact.toml")
	got, err := rootPath(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if got != root {
		t.Fatalf("rootPath(%q) = %q, want %q", configPath, got, root)
	}
	if _, err := readPolicy(got, configPath); err != nil {
		t.Fatal(err)
	}
}

func TestInitialPushAndRenamePaths(t *testing.T) {
	root := t.TempDir()
	initGitRepo(t, root)
	writeFixtureFile(t, root, "old/name.txt", "initial\n")
	initial := commitFixture(t, root, "initial")
	paths, err := changedPaths(root, strings.Repeat("0", 40), initial, "push")
	if err != nil || !reflect.DeepEqual(paths, []string{"old/name.txt"}) {
		t.Fatalf("initial push paths = %v, err = %v", paths, err)
	}

	if err := os.MkdirAll(filepath.Join(root, "new"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(filepath.Join(root, "old/name.txt"), filepath.Join(root, "new/name.txt")); err != nil {
		t.Fatal(err)
	}
	renamed := commitFixture(t, root, "rename")
	paths, err = changedPaths(root, initial, renamed, "push")
	if err != nil {
		t.Fatalf("rename diff: %v", err)
	}
	if !reflect.DeepEqual(paths, []string{"old/name.txt", "new/name.txt"}) {
		t.Fatalf("rename paths = %v", paths)
	}
}

func TestPullRequestUsesMergeBaseAndPushUsesExactEndpoints(t *testing.T) {
	root := t.TempDir()
	initGitRepo(t, root)
	writeFixtureFile(t, root, "common.txt", "common\n")
	common := commitFixture(t, root, "common")
	gitTest(t, root, "checkout", "-qb", "feature")
	writeFixtureFile(t, root, "feature.txt", "feature\n")
	feature := commitFixture(t, root, "feature change")

	gitTest(t, root, "checkout", "-q", common)
	gitTest(t, root, "checkout", "-qb", "base-update")
	writeFixtureFile(t, root, "base-only.txt", "base update\n")
	baseUpdate := commitFixture(t, root, "base update")

	pullRequestPaths, err := changedPaths(root, baseUpdate, feature, "pull_request")
	if err != nil {
		t.Fatalf("pull request diff: %v", err)
	}
	if !reflect.DeepEqual(pullRequestPaths, []string{"feature.txt"}) {
		t.Fatalf("pull request paths = %v", pullRequestPaths)
	}

	pushPaths, err := changedPaths(root, baseUpdate, feature, "push")
	if err != nil {
		t.Fatalf("push diff: %v", err)
	}
	if !reflect.DeepEqual(pushPaths, []string{"base-only.txt", "feature.txt"}) {
		t.Fatalf("push exact-endpoint paths = %v", pushPaths)
	}
}

func TestGitHubOutputsUseConfiguredJobsAndCollections(t *testing.T) {
	root := fixtureRepo(t)
	g, err := readPolicy(root, "ci-impact.toml")
	if err != nil {
		t.Fatal(err)
	}
	p, err := g.selectPaths([]string{"packages/base/src/index.ts", "native/app/main.go"})
	if err != nil {
		t.Fatal(err)
	}
	outputPath := filepath.Join(t.TempDir(), "github-output")
	if err := writeOutputs(outputPath, p); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(outputPath)
	if err != nil {
		t.Fatal(err)
	}
	want := "native=true\npolicy=false\nunit=false\nweb=true\nnative_languages=[\"go\"]\nnative_targets=[\"native/app\"]\npackages=[\"@test/app\",\"@test/base\"]\n"
	if string(raw) != want {
		t.Fatalf("GitHub outputs =\n%s\nwant =\n%s", raw, want)
	}
}
