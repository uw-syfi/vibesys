package main

import (
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
		{"unsafe path", strings.Replace(base, "python/service/handler.py", "../outside", 1), "unsafe path"},
		{"duplicate path", strings.Replace(base, `"python/service/handler.py"`, `"python/service/handler.py", "python/service/handler.py"`, 1), "duplicate entry"},
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
