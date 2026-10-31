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

// An entry of roots or files may be a table that lists a path without selecting from it. The
// derived replays use this to key their set on libraries a change to which should not run the
// client jobs; one list carries both tiers.
func appendComponent(t *testing.T, root, component string) {
	t.Helper()
	path := filepath.Join(root, "repoctl.toml")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, append(data, component...), 0600); err != nil {
		t.Fatal(err)
	}
}

func TestAKeyOnlyEntryIsListedButSelectsNothing(t *testing.T) {
	root := fixtureRepo(t)
	for _, dir := range []string{"live", "keyed"} {
		if err := os.Mkdir(filepath.Join(root, dir), 0700); err != nil {
			t.Fatal(err)
		}
	}
	for _, file := range []string{"live.txt", "keyed.txt"} {
		if err := os.WriteFile(filepath.Join(root, file), nil, 0600); err != nil {
			t.Fatal(err)
		}
	}
	appendComponent(t, root, `
[[components]]
id = "two_tiers"
class = "manual"
job = "web"
roots = ["live", { path = "keyed", selects = false }]
files = ["live.txt", { path = "keyed.txt", selects = false }]
`)
	g, err := readCurrentPolicy(root, "repoctl.toml")
	if err != nil {
		t.Fatal(err)
	}
	c := g.Components["two_tiers"]
	if !reflect.DeepEqual(c.Roots, []string{"live"}) || !reflect.DeepEqual(c.KeyOnlyRoots, []string{"keyed"}) {
		t.Fatalf("roots = %v, key-only roots = %v", c.Roots, c.KeyOnlyRoots)
	}
	if !reflect.DeepEqual(c.Files, []string{"live.txt"}) || !reflect.DeepEqual(c.KeyOnlyFiles, []string{"keyed.txt"}) {
		t.Fatalf("files = %v, key-only files = %v", c.Files, c.KeyOnlyFiles)
	}
	for _, path := range []string{"live/a.py", "live.txt"} {
		p, err := g.selectPaths([]string{path})
		if err != nil || !p.Jobs["web"] {
			t.Errorf("a change to %s selects web = %v (error %v), want true", path, p.Jobs["web"], err)
		}
	}
	// A path only a key-only entry names is owned by no component, so it selects nothing.
	for _, path := range []string{"keyed/a.py", "keyed.txt"} {
		if owners := g.owners(path); len(owners) != 0 {
			t.Errorf("%s is owned by %v, want no owner", path, owners)
		}
	}
}

func TestAKeyOnlyEntryMustStillExist(t *testing.T) {
	root := fixtureRepo(t)
	appendComponent(t, root, "\n[[components]]\nid = \"stale\"\nroots = [{ path = \"gone/path\", selects = false }]\n")
	_, err := readCurrentPolicy(root, "repoctl.toml")
	want := `component "stale" key-only roots: path "gone/path" does not exist`
	if err == nil || !strings.Contains(err.Error(), want) {
		t.Fatalf("readCurrentPolicy() error = %v; want %q", err, want)
	}
}

func TestAKeyOnlyEntryCannotRepeatASelectingOne(t *testing.T) {
	root := fixtureRepo(t)
	appendComponent(t, root, "\n[[components]]\nid = \"twice\"\nroots = [\"a\", { path = \"a\", selects = false }]\n")
	_, err := readPolicy(root, "repoctl.toml")
	if err == nil || !strings.Contains(err.Error(), "twice.roots") {
		t.Fatalf("readPolicy() error = %v; want a duplicate rejected in twice.roots", err)
	}
}

func TestAnEntryTableRejectsWhatItDoesNotUnderstand(t *testing.T) {
	for entry, want := range map[string]string{
		`{ path = "x", select = false }`: `unknown key "select"`,
		`{ selects = false }`:            "needs a string path",
		`{ path = "x", selects = "no" }`: "selects must be true or false",
		`42`:                             "must be a string or a table",
	} {
		t.Run(entry, func(t *testing.T) {
			root := fixtureRepo(t)
			appendComponent(t, root, "\n[[components]]\nid = \"bad\"\nroots = ["+entry+"]\n")
			_, err := readPolicy(root, "repoctl.toml")
			if err == nil || !strings.Contains(err.Error(), want) {
				t.Fatalf("readPolicy() error = %v; want %q", err, want)
			}
		})
	}
}
