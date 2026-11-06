package archcheck_test

import (
	"golang.org/x/tools/go/packages"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/archcheck"
)

func goFile(imports ...string) string {
	var b strings.Builder
	b.WriteString("package p\n\nimport (\n")
	for _, target := range imports {
		b.WriteString("\t_ \"" + target + "\"\n")
	}
	b.WriteString(")\n")
	return b.String()
}

const module = "github.com/uw-syfi/vibesys/orchestrator/"

func TestRules(t *testing.T) {
	cases := []struct {
		name  string
		files map[string]string
		bad   bool
	}{
		{"core imports values from the standard library", map[string]string{"orchestrator/internal/core/a.go": goFile("time", "slices")}, false},
		{"core cannot import generated types", map[string]string{"orchestrator/internal/core/a.go": goFile(module + "gen/vibesys/orchestrator/v1")}, true},
		{"core cannot import the wire", map[string]string{"orchestrator/internal/core/a.go": goFile(module + "internal/wire")}, true},
		{"core cannot do I/O", map[string]string{"orchestrator/internal/core/a.go": goFile("os")}, true},
		{"core cannot draw random numbers", map[string]string{"orchestrator/internal/core/a.go": goFile("math/rand/v2")}, true},
		{"core cannot start goroutines' helpers", map[string]string{"orchestrator/internal/core/a.go": goFile("sync")}, true},
		{"core cannot read the clock", map[string]string{"orchestrator/internal/core/a.go": goFile("time") + "var t = time.Now()\n"}, true},
		{"core cannot wait", map[string]string{"orchestrator/internal/core/a.go": goFile("time") + "var c = time.After(0)\n"}, true},
		{"a core test may read what it likes", map[string]string{"orchestrator/internal/core/a_test.go": goFile("math/rand/v2", "os")}, false},
		{"the simulation replays through the wire", map[string]string{"orchestrator/internal/core/a_test.go": goFile(module + "internal/wire")}, false},
		{"the wire imports core and gen", map[string]string{"orchestrator/internal/wire/a.go": goFile(module+"internal/core", module+"gen/vibesys/orchestrator/v1")}, false},
		{"jobs cannot import the wire", map[string]string{"orchestrator/internal/jobs/a.go": goFile(module + "internal/wire")}, true},
		{"jobs cannot import generated types", map[string]string{"orchestrator/internal/jobs/a.go": goFile(module + "gen/vibesys/orchestrator/v1")}, true},
		{"generated code imports only generated code", map[string]string{"orchestrator/gen/vibesys/orchestrator/v1/a.go": goFile(module + "internal/core")}, true},
		{"a package with no rule fails", map[string]string{"orchestrator/internal/newthing/a.go": goFile("time")}, true},
		{"nothing imports the CLI", map[string]string{"orchestrator/internal/wire/a.go": goFile("github.com/uw-syfi/vibesys/cli/internal/core")}, true},
		{"nothing imports the gateway", map[string]string{"orchestrator/internal/wire/a_test.go": goFile("github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1")}, true},
		{"a store test may use the fake detector", map[string]string{"orchestrator/internal/store/a_test.go": goFile(module + "internal/localfs/localfstest")}, false},
		{"store code cannot use the fake detector", map[string]string{"orchestrator/internal/store/a.go": goFile(module + "internal/localfs/localfstest")}, true},
		{"the contract suite package cannot use the fake detector", map[string]string{"orchestrator/internal/store/storetest/a.go": goFile(module + "internal/localfs/localfstest")}, true},
		{"the daemon cannot read the clock", map[string]string{"orchestrator/internal/daemon/a.go": goFile("time") + "var t = time.Now()\n"}, true},
		{"the daemon cannot start processes", map[string]string{"orchestrator/internal/daemon/a.go": goFile("os/exec")}, true},
		{"the client cannot draw random numbers", map[string]string{"orchestrator/internal/client/a.go": goFile("crypto/rand")}, true},
		{"production may read the clock", map[string]string{"orchestrator/internal/production/a.go": goFile("time") + "var t = time.Now()\n"}, false},
		{"a daemon test may wait with a guard", map[string]string{"orchestrator/internal/daemon/a_test.go": goFile("time") + "var c = time.After(0)\n"}, false},
		{"the view imports the messages and values-only standard library", map[string]string{"orchestrator/clientcli/attachview/a.go": goFile("fmt", "time", "slices", "google.golang.org/protobuf/proto", module+"gen/vibesys/orchestrator/v1", "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1")}, false},
		{"the view cannot write", map[string]string{"orchestrator/clientcli/attachview/a.go": goFile("os")}, true},
		{"the view cannot hold a stream", map[string]string{"orchestrator/clientcli/attachview/a.go": goFile("google.golang.org/grpc")}, true},
		{"the view cannot wait on anything", map[string]string{"orchestrator/clientcli/attachview/a.go": goFile("context")}, true},
		{"the view cannot use a lock", map[string]string{"orchestrator/clientcli/attachview/a.go": goFile("sync")}, true},
		{"the view cannot reach the service client", map[string]string{"orchestrator/clientcli/attachview/a.go": goFile(module + "internal/client")}, true},
		{"the view cannot import the verbs", map[string]string{"orchestrator/clientcli/attachview/a.go": goFile(module + "clientcli")}, true},
		{"the view cannot read the clock", map[string]string{"orchestrator/clientcli/attachview/a.go": goFile("time") + "var t = time.Now()\n"}, true},
		{"the verbs import the view", map[string]string{"orchestrator/clientcli/a.go": goFile(module + "clientcli/attachview")}, false},
		{"another package cannot spell the manifest's name", map[string]string{"orchestrator/internal/production/a.go": "package p\n\nvar manifest = \"release.json\"\n"}, true},
		{"another package cannot spell the environment folder", map[string]string{"orchestrator/hosted/a.go": "package p\n\nvar python = \"sidecar/.venv/bin/python\"\n"}, true},
		{"no process-table scraping", map[string]string{"orchestrator/hosted/a.go": "package p\n\nvar tool = \"pgrep\"\n"}, true},
		{"no ps", map[string]string{"orchestrator/hosted/a.go": "package p\n\nvar tool = \"ps\"\n"}, true},
		{"no command line files", map[string]string{"orchestrator/hosted/a.go": "package p\n\nvar f = \"/proc/1/cmdline\"\n"}, true},
		{"package release owns the layout", map[string]string{"orchestrator/release/a.go": "package p\n\nvar python = \"sidecar/.venv/bin/python\"\nvar manifest = \"release.json\"\n"}, false},
		{"a comment may name the manifest", map[string]string{"orchestrator/internal/production/a.go": "package p\n\n// RELEASE/release.json\nvar x = 1\n"}, false},
		{"a test may spell the manifest's name", map[string]string{"orchestrator/internal/production/a_test.go": "package p\n\nvar manifest = \"release.json\"\n"}, false},
		{"an unparsable file fails", map[string]string{"orchestrator/internal/core/a.go": "package ("}, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := archcheck.Violations(c.files)
			if c.bad != (len(got) > 0) {
				t.Fatalf("violations = %v, want bad = %v", got, c.bad)
			}
		})
	}
}

// The real tree obeys the rules.
func TestRepositoryObeysTheRules(t *testing.T) {
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	files := map[string]string{}
	err = filepath.WalkDir(filepath.Join(root, "orchestrator"), func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || path.Ext(p) != ".go" {
			return err
		}
		source, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		files[filepath.ToSlash(rel)] = string(source)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(files) == 0 {
		t.Fatal("found no sources to check")
	}
	for _, v := range archcheck.Violations(files) {
		t.Error(v)
	}
}

// The installer asks the host through the client and reads no process list.
func TestInstallerDoesNotScrapeTheProcessTable(t *testing.T) {
	source, err := os.ReadFile("../../../install.sh")
	if err != nil {
		t.Fatal(err)
	}
	for number, line := range strings.Split(string(source), "\n") {
		code, _, _ := strings.Cut(line, "#")
		for _, word := range strings.Fields(code) {
			if word == "ps" || word == "pgrep" || word == "pkill" || strings.Contains(word, "/proc/") {
				t.Errorf("install.sh:%d reads the process table (%q); ask the host through `vibesys-client host stop`", number+1, word)
			}
		}
	}
}

// Include gateway e2e readers, whose build tag otherwise hides them from ordinary checks.
func TestTypedWatchRunsReaders(t *testing.T) {
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	foundImport, foundGateway := false, false
	for _, dir := range []string{"orchestrator", "gateway"} {
		pkgs, err := packages.Load(&packages.Config{Dir: filepath.Join(root, dir), Tests: true, BuildFlags: []string{"-tags=e2e"}, Mode: packages.NeedName | packages.NeedFiles | packages.NeedCompiledGoFiles | packages.NeedSyntax | packages.NeedTypes | packages.NeedTypesInfo | packages.NeedImports | packages.NeedDeps}, "./...")
		if err != nil {
			t.Fatal(err)
		}
		if packages.PrintErrors(pkgs) > 0 {
			t.Fatal("cannot type-check readers")
		}
		for _, pkg := range pkgs {
			if _, ok := pkg.Imports[module+"gen/vibesys/orchestrator/v1"]; ok {
				foundImport = true
			}
			for _, file := range pkg.Syntax {
				name, err := filepath.Rel(root, pkg.Fset.Position(file.Pos()).Filename)
				if err != nil {
					t.Fatal(err)
				}
				if strings.HasPrefix(filepath.ToSlash(name), "gateway/e2e/") {
					foundGateway = true
				}
				for _, violation := range archcheck.WatchRunsReaders(filepath.ToSlash(name), file, pkg.Fset, pkg.TypesInfo) {
					t.Error(violation)
				}
			}
		}
	}
	if !foundImport {
		t.Error("no loaded package imports the orchestrator proto package")
	}
	if !foundGateway {
		t.Error("no gateway/e2e files examined")
	}
}
