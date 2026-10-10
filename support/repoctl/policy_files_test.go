package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func splitFixturePolicy(t *testing.T) string {
	t.Helper()
	root := fixtureRepo(t)
	contents, err := os.ReadFile(filepath.Join(root, "repoctl.toml"))
	if err != nil {
		t.Fatal(err)
	}
	text := string(contents)
	checksStart := strings.Index(text, "[native_checks]")
	componentsEnd := strings.Index(text, "[[components]]")
	if checksStart < 0 || componentsEnd < checksStart {
		t.Fatal("fixture sections are out of order")
	}
	configDir := filepath.Join(root, ".repoctl")
	if err := os.Mkdir(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string]string{
		"components.toml": text[:checksStart] + text[componentsEnd:],
		"checks.toml":     text[checksStart:componentsEnd],
	} {
		if err := os.WriteFile(filepath.Join(configDir, name), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestSplitPolicyMatchesSingleFile(t *testing.T) {
	root := splitFixturePolicy(t)
	legacy, err := readPolicy(root, "repoctl.toml")
	if err != nil {
		t.Fatal(err)
	}
	split, err := readPolicy(root, ".repoctl")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(split, legacy) {
		t.Fatal("split policy differs from legacy policy")
	}
	gotRoot, err := rootPath(filepath.Join(root, ".repoctl"))
	if err != nil || gotRoot != root {
		t.Fatalf("absolute policy directory root = %q, %v; want %q", gotRoot, err, root)
	}
}

func TestSplitPolicyRejectsInvalidFiles(t *testing.T) {
	for _, tc := range []struct {
		name, file, prepend, replaceOld, replaceNew, want string
		remove                                            bool
	}{
		{name: "missing components", file: "components.toml", remove: true, want: "components.toml"},
		{name: "missing checks", file: "checks.toml", remove: true, want: "checks.toml"},
		{name: "missing component field", file: "components.toml", replaceOld: `default_base = "trunk"`, want: "missing key default_base"},
		{name: "unknown component key", file: "components.toml", prepend: "typo = 1\n", want: "unknown keys"},
		{name: "check key in components", file: "components.toml", prepend: "check_groups = []\n", want: "belongs in another config file"},
		{name: "component key in checks", file: "checks.toml", prepend: "jobs = []\n", want: "belongs in another config file"},
		{name: "unknown check job", file: "checks.toml", replaceOld: `trigger_job = "unit"`, replaceNew: `trigger_job = "missing"`, want: "unknown trigger_job"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := splitFixturePolicy(t)
			path := filepath.Join(root, ".repoctl", tc.file)
			if tc.remove {
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
			} else {
				data, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				contents := tc.prepend + string(data)
				if tc.replaceOld != "" {
					contents = strings.Replace(contents, tc.replaceOld, tc.replaceNew, 1)
				}
				if err := os.WriteFile(path, []byte(contents), 0600); err != nil {
					t.Fatal(err)
				}
			}
			_, err := readPolicy(root, ".repoctl")
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("readPolicy() error = %v; want %q", err, tc.want)
			}
		})
	}
}

// A declared component path that is absent from the repository would never
// match a changed file, so loading must name the component, key, and path.
func TestPolicyRejectsMissingComponentPaths(t *testing.T) {
	for _, class := range []string{"", "manual", "python_support"} {
		for _, key := range []string{"roots", "files"} {
			for _, shape := range []string{"only", "after_existing"} {
				name := class + "/" + key + "/" + shape
				t.Run(name, func(t *testing.T) {
					root := fixtureRepo(t)
					paths := `"gone/path"`
					if shape == "after_existing" {
						if err := os.WriteFile(filepath.Join(root, "present.txt"), nil, 0600); err != nil {
							t.Fatal(err)
						}
						paths = `"present.txt", "gone/path"`
					}
					classLine := ""
					if class != "" {
						classLine = "class = \"" + class + "\"\n"
					}
					component := "\n[[components]]\nid = \"stale\"\n" + classLine + key + " = [" + paths + "]\n"
					path := filepath.Join(root, "repoctl.toml")
					data, err := os.ReadFile(path)
					if err != nil {
						t.Fatal(err)
					}
					if err := os.WriteFile(path, append(data, component...), 0600); err != nil {
						t.Fatal(err)
					}
					_, err = readCurrentPolicy(root, "repoctl.toml")
					want := `component "stale" ` + key + `: path "gone/path" does not exist`
					if err == nil || !strings.Contains(err.Error(), want) {
						t.Fatalf("readCurrentPolicy() error = %v; want %q", err, want)
					}
				})
			}
		}
	}
}

// The base of a comparison is a historical snapshot: it may still declare paths
// that were later removed, and must stay loadable so the change can be planned.
func TestPolicySnapshotAllowsMissingComponentPaths(t *testing.T) {
	root := fixtureRepo(t)
	path := filepath.Join(root, "repoctl.toml")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	stale := append(data, "\n[[components]]\nid = \"stale\"\nfiles = [\"gone/path\"]\n"...)
	if err := os.WriteFile(path, stale, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := readPolicy(root, "repoctl.toml"); err != nil {
		t.Fatalf("readPolicy() = %v; want a historical snapshot to load", err)
	}
}
