package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writePolicyCases(t *testing.T, root, contents string) string {
	t.Helper()
	path := filepath.Join(root, "policy-cases.toml")
	if err := os.WriteFile(path, []byte(contents), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestVerifyPolicyCases(t *testing.T) {
	root := fixtureRepo(t)
	g, err := readPolicy(root, "repoctl.toml")
	if err != nil {
		t.Fatal(err)
	}
	path := writePolicyCases(t, root, `[[cases]]
name = "python service"
paths = ["python/service/handler.py"]
jobs = ["unit"]
local_groups = ["unit"]
checks_groups = []
[cases.collections]
native_targets = []
native_languages = []
packages = []

[[cases]]
name = "native child closure"
paths = ["native/engine/child/go.mod"]
jobs = ["native"]
[cases.collections]
native_targets = ["native/engine", "native/engine/child"]
native_languages = ["go", "rust"]
packages = []
`)
	cases, err := readPolicyCases(path, g)
	if err != nil {
		t.Fatal(err)
	}
	if err := verifyPolicyCases(g, cases); err != nil {
		t.Fatal(err)
	}
	if err := runVerifyPolicy(root, g, []string{"--cases", "policy-cases.toml"}); err != nil {
		t.Fatal(err)
	}
}

func TestVerifyPolicyCasesRejectsInvalidContracts(t *testing.T) {
	root := fixtureRepo(t)
	g, err := readPolicy(root, "repoctl.toml")
	if err != nil {
		t.Fatal(err)
	}
	base := `[[cases]]
name = "case"
paths = ["python/service/handler.py"]
jobs = ["unit"]
[cases.collections]
native_targets = []
native_languages = []
packages = []
`
	tests := []struct {
		name, contents, want string
	}{
		{"empty file", "", "cases must not be empty"},
		{"unknown key", strings.Replace(base, "[cases.collections]", "extra = true\n[cases.collections]", 1), "unknown keys"},
		{"missing paths", strings.Replace(base, "paths = [\"python/service/handler.py\"]\n", "", 1), "paths must not be empty"},
		{"missing jobs", strings.Replace(base, "jobs = [\"unit\"]\n", "", 1), "jobs is required"},
		{"missing collection", strings.Replace(base, "packages = []\n", "", 1), "missing collection"},
		{"unknown job", strings.Replace(base, "jobs = [\"unit\"]", "jobs = [\"missing\"]", 1), "unknown job"},
		{"unknown local group", strings.Replace(base, "[cases.collections]", "local_groups = [\"missing\"]\n[cases.collections]", 1), "unknown group"},
		{"unsafe path", strings.Replace(base, "python/service/handler.py", "../outside", 1), "unsafe path"},
		{"duplicate path", strings.Replace(base, `"python/service/handler.py"`, `"python/service/handler.py", "python/service/handler.py"`, 1), "duplicate entry"},
		{"unknown intent", strings.Replace(base, "packages = []", `packages = "everything"`, 1), "unknown collection intent"},
		{"unknown intent key", strings.Replace(base, "packages = []", `packages = { dependent_of = "pkg:@test/base" }`, 1), "dependent_of"},
		{"extra intent key", strings.Replace(base, "packages = []", `packages = { dependents_of = "pkg:@test/base", extra = true }`, 1), "extra"},
		{"unknown source", strings.Replace(base, "packages = []", `packages = { dependents_of = "pkg:missing" }`, 1), "pkg:missing"},
		{"empty source", strings.Replace(base, "packages = []", `packages = { dependents_of = "" }`, 1), "requires a component ID"},
		{"nonstring source", strings.Replace(base, "packages = []", `packages = { dependents_of = 1 }`, 1), "requires a component ID"},
		{"empty intent", strings.Replace(base, "packages = []", `packages = {}`, 1), "requires a component ID"},
		{"nonstring entry", strings.Replace(base, "packages = []", `packages = [1]`, 1), "entries must be strings"},
		{"invalid type", strings.Replace(base, "packages = []", `packages = true`, 1), "collection must be"},
		{"duplicate value", strings.Replace(base, "packages = []", `packages = ["a", "a"]`, 1), "duplicate entry"},

		{"duplicate name", base + base, "duplicate case name"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := writePolicyCases(t, root, tt.contents)
			_, err := readPolicyCases(path, g)
			if err == nil || !strings.Contains(err.Error(), tt.want) {
				t.Fatalf("error = %v, want %q", err, tt.want)
			}
		})
	}
}

func TestVerifyPolicyCasesDetectsSelectionDrift(t *testing.T) {
	root := fixtureRepo(t)
	g, err := readPolicy(root, "repoctl.toml")
	if err != nil {
		t.Fatal(err)
	}
	for _, tt := range []struct {
		name, jobs, targets, want string
	}{
		{"missing selected job", "[]", "[]", "jobs ="},
		{"unexpected selected job", `["unit", "native"]`, "[]", "jobs ="},
		{"missing selected target", `["native"]`, "[]", "collection \"native_targets\""},
	} {
		t.Run(tt.name, func(t *testing.T) {
			path := writePolicyCases(t, root, `[[cases]]
name = "drift"
paths = ["native/app/main.go"]
jobs = `+tt.jobs+`
[cases.collections]
native_targets = `+tt.targets+`
native_languages = ["go"]
packages = []
`)
			cases, err := readPolicyCases(path, g)
			if err != nil {
				t.Fatal(err)
			}
			if err := verifyPolicyCases(g, cases); err == nil || !strings.Contains(err.Error(), tt.want) {
				t.Fatalf("error = %v, want %q", err, tt.want)
			}
		})
	}
}

func TestVerifyPolicyCollectionIntentsFollowGraphGrowth(t *testing.T) {
	for _, count := range []int{0, 1, 5} {
		for _, intent := range []string{`"all"`, `{ dependents_of = "pkg:@test/base" }`} {
			t.Run(fmt.Sprintf("%s/%d", intent, count), func(t *testing.T) {
				root := fixtureRepo(t)
				previous := "@test/app"
				for i := 0; i < count; i++ {
					name := fmt.Sprintf("@test/consumer-%d", i)
					writeFixtureFile(t, root, fmt.Sprintf("packages/consumer-%d/package.json", i),
						fmt.Sprintf(`{"name":%q,"devDependencies":{%q:"workspace:*"}}`, name, previous))
					previous = name
				}
				writeFixtureFile(t, root, "other/unselected/go.mod", "module example.test/unselected\n")
				// Only the all intent includes this unrelated package.
				writeFixtureFile(t, root, "packages/unrelated/package.json", `{"name":"@test/unrelated"}`)
				path, jobs, native := "packages/base/src/index.ts", `["web"]`, `[]`
				if intent == `"all"` {
					path, jobs, native = "repoctl.toml", `["unit", "web", "native", "policy"]`, `"all"`
				}
				writePolicyCases(t, root, fmt.Sprintf(`[[cases]]
name = "graph growth"
paths = [%q]
jobs = %s
[cases.collections]
native_targets = %s
native_languages = %s
packages = %s
`, path, jobs, native, native, intent))
				g, err := readPolicy(root, "repoctl.toml")
				if err != nil {
					t.Fatal(err)
				}
				if err := runVerifyPolicy(root, g, []string{"--cases", "policy-cases.toml"}); err != nil {
					t.Fatal(err)
				}
			})
		}
	}
}

func TestVerifyPolicyCollectionIntentsDetectDrift(t *testing.T) {
	for _, tt := range []struct{ name, intent, path, jobs, native, want string }{
		{"all misses package", `"all"`, "packages/base/index.ts", `["web"]`, `[]`, "missing [@test/unrelated]"},
		{"dependents misses source", `{ dependents_of = "pkg:@test/base" }`, "packages/app/index.ts", `["web"]`, `[]`, "missing [@test/base]"},
		{"dependents extra package", `{ dependents_of = "pkg:@test/base" }`, "repoctl.toml", `["unit", "web", "native", "policy"]`, `"all"`, "extra [@test/unrelated]"},
	} {
		t.Run(tt.name, func(t *testing.T) {
			root := fixtureRepo(t)
			writeFixtureFile(t, root, "packages/unrelated/package.json", `{"name":"@test/unrelated"}`)
			g, err := readPolicy(root, "repoctl.toml")
			if err != nil {
				t.Fatal(err)
			}
			writePolicyCases(t, root, fmt.Sprintf(`[[cases]]
name = "drift"
paths = [%q]
jobs = %s
[cases.collections]
native_targets = %s
native_languages = %s
packages = %s
`, tt.path, tt.jobs, tt.native, tt.native, tt.intent))
			err = runVerifyPolicy(root, g, []string{"--cases", "policy-cases.toml"})
			if err == nil || !strings.Contains(err.Error(), tt.want) {
				t.Fatalf("error = %v, want %q", err, tt.want)
			}
		})
	}
}

func TestVerifyPolicyDependentsProjectAcrossClasses(t *testing.T) {
	root := fixtureRepo(t)
	policyPath := filepath.Join(root, "repoctl.toml")
	policy, err := os.ReadFile(policyPath)
	if err != nil {
		t.Fatal(err)
	}
	policy = append(policy, []byte(`
[[edges]]
from = "protocol"
to = "pkg:@test/base"
`)...)
	if err := os.WriteFile(policyPath, policy, 0600); err != nil {
		t.Fatal(err)
	}
	g, err := readPolicy(root, "repoctl.toml")
	if err != nil {
		t.Fatal(err)
	}
	writePolicyCases(t, root, `[[cases]]
name = "project non-package source"
paths = ["shared/protocol.txt"]
jobs = ["web"]
[cases.collections]
native_targets = { dependents_of = "protocol" }
native_languages = []
packages = { dependents_of = "protocol" }
`)
	if err := runVerifyPolicy(root, g, []string{"--cases", "policy-cases.toml"}); err != nil {
		t.Fatal(err)
	}
}
