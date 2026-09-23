package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func repoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	return root
}
func TestRealGraphEffects(t *testing.T) {
	g, err := readPolicy(repoRoot(t))
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		path      string
		job       string
		native    []string
		languages []string
		packages  []string
	}{
		{"src/server/api/schema.py", "tui", nil, nil, nil},
		{"sdk/vs-evaluator/vseval/go.mod", "evaluators", []string{"resources/evaluators/microservice", "resources/evaluators/queue", "sdk/vs-evaluator/vseval"}, []string{"go"}, nil},
		{"resources/evaluators/queue/native_runner/Cargo.toml", "evaluators", []string{"resources/evaluators/queue", "resources/evaluators/queue/native_runner"}, []string{"go", "rust"}, nil},
		{"clients/backend-client/src/index.ts", "tui", nil, nil, []string{"@vibesys/backend-client", "@vibesys/core-state", "@vibesys/tui", "@vibesys/web"}},
		{"examples/microservices/hotel-correctness/.vibesys/tasks/compose/evaluator/go.mod", "examples", nil, nil, nil},
	}
	for _, tt := range tests {
		t.Run(tt.path, func(t *testing.T) {
			p, err := g.selectPaths([]string{tt.path})
			if err != nil {
				t.Fatal(err)
			}
			if !p.Jobs[tt.job] {
				t.Errorf("expected %s job", tt.job)
			}
			if tt.native != nil && !reflect.DeepEqual(p.NativeTargets, tt.native) {
				t.Errorf("native targets %v", p.NativeTargets)
			}
			if tt.languages != nil && !reflect.DeepEqual(p.NativeLanguages, tt.languages) {
				t.Errorf("native languages %v", p.NativeLanguages)
			}
			if tt.packages != nil && !reflect.DeepEqual(p.PnpmPackages, tt.packages) {
				t.Errorf("packages %v", p.PnpmPackages)
			}
		})
	}
	p, err := g.selectPaths([]string{"unknown/new.py"})
	if err == nil || !strings.Contains(err.Error(), "unowned") {
		t.Fatalf("unknown path: %v, %v", p, err)
	}
}
func TestReverseClosure(t *testing.T) {
	g := graph{Components: map[string]component{}, Order: []string{}}
	for _, c := range []component{{ID: "a", Files: []string{"a.py"}}, {ID: "b", DependsOn: []string{"a"}}, {ID: "c", DependsOn: []string{"b"}, Jobs: []string{"python"}}} {
		if err := g.add(c); err != nil {
			t.Fatal(err)
		}
	}
	p, err := g.selectPaths([]string{"a.py"})
	if err != nil {
		t.Fatal(err)
	}
	if !p.Jobs["python"] || !strings.Contains(p.Components["c"][0], "depends on b") {
		t.Fatal(p)
	}
}
func git(t *testing.T, root string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = root
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v: %s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}
func TestGitDiffSemantics(t *testing.T) {
	root := t.TempDir()
	git(t, root, "init", "-q")
	git(t, root, "config", "user.email", "ci@example.invalid")
	git(t, root, "config", "user.name", "CI")
	if err := os.WriteFile(filepath.Join(root, "before.py"), []byte("content\n"), 0600); err != nil {
		t.Fatal(err)
	}
	git(t, root, "add", ".")
	git(t, root, "commit", "-qm", "initial")
	base := git(t, root, "rev-parse", "HEAD")
	initial, err := changedPaths(root, strings.Repeat("0", 40), base, "push")
	if err != nil || !reflect.DeepEqual(initial, []string{"before.py"}) {
		t.Fatalf("initial: %v %v", initial, err)
	}
	if err := os.Rename(filepath.Join(root, "before.py"), filepath.Join(root, "after.py")); err != nil {
		t.Fatal(err)
	}
	git(t, root, "add", "-A")
	git(t, root, "commit", "-qm", "rename")
	head := git(t, root, "rev-parse", "HEAD")
	paths, err := changedPaths(root, base, head, "push")
	if err != nil || !reflect.DeepEqual(paths, []string{"before.py", "after.py"}) {
		t.Fatalf("rename: %v %v", paths, err)
	}
}
func TestCLIAndOutputs(t *testing.T) {
	t.Setenv("CI_IMPACT_ROOT", repoRoot(t))
	if err := cli([]string{"explain", "src/server/api/schema.py"}); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "output")
	if err := cli([]string{"plan", "--base", "HEAD", "--head", "HEAD", "--github-output", path}); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	for _, fragment := range []string{"python=false", "go_prototype=false", "native_targets=[]", "native_languages=[]", "pnpm_packages=[]"} {
		if !strings.Contains(string(raw), fragment) {
			t.Errorf("missing %s from %q", fragment, raw)
		}
	}
}
