package archcheck_test

import (
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/archcheck"
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

const module = "github.com/uw-syfi/vibesys/cli/"

func TestLayerRules(t *testing.T) {
	cases := []struct {
		name  string
		files map[string]string
		bad   bool
	}{
		{"core imports only the standard library", map[string]string{"cli/internal/core/a.go": goFile("time")}, false},
		{"core cannot import the store", map[string]string{"cli/internal/core/a.go": goFile(module + "internal/store")}, true},
		{"the store imports core", map[string]string{"cli/internal/store/a.go": goFile(module + "internal/core")}, false},
		{"core cannot import generated types", map[string]string{"cli/internal/core/a.go": goFile(module + "gen/vibesys/cli/v1")}, true},
		{"wire imports core and gen", map[string]string{"cli/internal/wire/a.go": goFile(module+"internal/core", module+"gen/vibesys/cli/v1")}, false},
		{"the store cannot import generated types", map[string]string{"cli/internal/store/a.go": goFile(module + "gen/vibesys/cli/v1")}, true},
		{"the environment cannot import the store", map[string]string{"cli/internal/environment/a.go": goFile(module + "internal/store")}, true},
		{"filestore imports the store interface", map[string]string{"cli/internal/store/filestore/a.go": goFile(module + "internal/store")}, false},
		{"the store cannot import its implementation", map[string]string{"cli/internal/store/a.go": goFile(module + "internal/store/filestore")}, true},
		{"generated code imports only generated code", map[string]string{"cli/gen/vibesys/cli/v1/a.go": goFile(module + "internal/core")}, true},
		{"a package with no rule fails", map[string]string{"cli/internal/newthing/a.go": goFile("time")}, true},
		{"ledger imports core and the store", map[string]string{"cli/internal/store/ledger/a.go": goFile(module+"internal/core", module+"internal/store")}, false},
		{"the ledger cannot import an implementation", map[string]string{"cli/internal/store/ledger/a.go": goFile(module + "internal/store/memstore")}, true},
		{"memstore imports the ledger", map[string]string{"cli/internal/store/memstore/a.go": goFile(module + "internal/store/ledger")}, false},
		{"filestore imports the ledger and its filesystem seam", map[string]string{"cli/internal/store/filestore/a.go": goFile(module+"internal/store/ledger", module+"internal/store/filestore/fsys")}, false},
		{"the filesystem seam imports nothing of the module", map[string]string{"cli/internal/store/filestore/fsys/a.go": goFile(module + "internal/core")}, true},
		{"the ledger cannot import the filesystem seam", map[string]string{"cli/internal/store/ledger/a.go": goFile(module + "internal/store/filestore/fsys")}, true},
		{"memstore and filestore cannot import each other", map[string]string{"cli/internal/store/memstore/a.go": goFile(module + "internal/store/filestore")}, true},
		{"filestore code cannot import the contract suite", map[string]string{"cli/internal/store/filestore/a.go": goFile(module + "internal/store/storetest")}, true},
		{"memstore code cannot import the contract suite", map[string]string{"cli/internal/store/memstore/a.go": goFile(module + "internal/store/storetest")}, true},
		{"a memstore test runs the contract suite", map[string]string{"cli/internal/store/memstore/a_test.go": goFile(module + "internal/store/storetest")}, false},
		{"a filestore test runs the suite and compares with memstore", map[string]string{"cli/internal/store/filestore/a_test.go": goFile(module+"internal/store/storetest", module+"internal/store/memstore")}, false},
		{"a memstore test cannot import filestore", map[string]string{"cli/internal/store/memstore/a_test.go": goFile(module + "internal/store/filestore")}, true},
		{"the contract suite imports core and the store", map[string]string{"cli/internal/store/storetest/a.go": goFile(module+"internal/core", module+"internal/store")}, false},
		{"the contract suite cannot import an implementation", map[string]string{"cli/internal/store/storetest/a.go": goFile(module + "internal/store/memstore")}, true},
		{"the command line cannot import the domain", map[string]string{"cli/cmd/vibesys-cli/a.go": goFile(module + "internal/core")}, true},
		{"the command line imports the client", map[string]string{"cli/cmd/vibesys-cli/a.go": goFile(module+"internal/client", module+"gen/vibesys/cli/v1", module+"internal/wire")}, false},
		{"the command line cannot import the daemon", map[string]string{"cli/cmd/vibesys-cli/a.go": goFile(module + "internal/daemon")}, true},
		{"the client cannot import the domain", map[string]string{"cli/internal/client/a.go": goFile(module + "internal/core")}, true},
		{"the client cannot import the daemon", map[string]string{"cli/internal/client/a.go": goFile(module + "internal/daemon")}, true},
		{"the daemon cannot import the client", map[string]string{"cli/internal/daemon/a.go": goFile(module + "internal/client")}, true},
		{"the daemon imports the job flows", map[string]string{"cli/internal/daemon/a.go": goFile(module+"internal/app", module+"internal/wire")}, false},
		{"the job flows cannot import the daemon", map[string]string{"cli/internal/app/a.go": goFile(module + "internal/daemon")}, true},
		{"the job flows cannot import generated types", map[string]string{"cli/internal/app/a.go": goFile(module + "gen/vibesys/cli/v1")}, true},
		{"the lifecycle machine cannot import the store", map[string]string{"cli/internal/lifecycle/a.go": goFile(module + "internal/store")}, true},
		{"the domain cannot import the lifecycle machine", map[string]string{"cli/internal/core/a.go": goFile(module + "internal/lifecycle")}, true},
		{"a supervisor cannot import the daemon", map[string]string{"cli/internal/supervisor/a.go": goFile(module + "internal/daemon")}, true},
		{"the supervisor domain cannot import its transport", map[string]string{"cli/internal/supervisor/a.go": goFile(module + "internal/supervisor/rpc")}, true},
		{"the transport imports the supervisor domain", map[string]string{"cli/internal/supervisor/rpc/a.go": goFile(module+"internal/supervisor", module+"internal/wire")}, false},
		{"the wiring imports the daemon and the client", map[string]string{"cli/internal/production/a.go": goFile(module+"internal/daemon", module+"internal/client")}, false},
		{"nothing but the wiring and the command line imports the wiring", map[string]string{"cli/internal/daemon/a.go": goFile(module + "internal/production")}, true},
		{"a daemon test composes fakes", map[string]string{"cli/internal/daemon/a_test.go": goFile(module + "internal/environment/fakeenv")}, false},
		{"daemon code cannot import a fake", map[string]string{"cli/internal/daemon/a.go": goFile(module + "internal/environment/fakeenv")}, true},
		{"the wiring cannot import a fake", map[string]string{"cli/internal/production/a.go": goFile(module + "internal/environment/fakeenv")}, true},
		{"the wiring cannot import the test daemon", map[string]string{"cli/internal/production/a.go": goFile(module + "internal/daemon/daemontest")}, true},
		{"the command line's tests compose a daemon", map[string]string{"cli/cmd/vibesys-cli/a_test.go": goFile(module+"internal/daemon/daemontest", module+"internal/core")}, false},
		{"an unparsable file fails", map[string]string{"cli/internal/core/a.go": "package ("}, true},
	}
	for _, c := range cases {
		if got := archcheck.Violations(c.files); (len(got) > 0) != c.bad {
			t.Errorf("%s: violations = %v, want bad=%v", c.name, got, c.bad)
		}
	}
}

// Whatever names the layout is flagged outside filestore and allowed inside it, in Go and proto.
func TestLayoutNamesStayInsideFilestore(t *testing.T) {
	names := []string{
		`".vibesys-state"`, `"events.jsonl"`, `"~/.local/state/vibesys"`, `os.Getenv("XDG_STATE_HOME")`,
		`"results/" + key`, `filepath.Join(dir, "runs", id)`, `path.Join("logs", id)`,
		`"jobs/" + id`, `"plans/1.json"`, `path.Join(root, "promotions")`, `filepath.Join(dir, "jobs", id)`,
	}
	property := func(pick uint8, before, after string) bool {
		source := "package p\n\nvar _ = " + before + names[int(pick)%len(names)] + after + "\n"
		outside := archcheck.Violations(map[string]string{"cli/internal/core/a.go": source})
		inside := archcheck.Violations(map[string]string{"cli/internal/store/filestore/a.go": source})
		return hasLayout(outside) && !hasLayout(inside)
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
	proto := archcheck.Violations(map[string]string{"proto/vibesys/cli/v1/a.proto": "// stored in events.jsonl\n"})
	if !hasLayout(proto) {
		t.Errorf("a proto comment naming the layout was not flagged: %v", proto)
	}
}

func hasLayout(violations []string) bool {
	for _, line := range violations {
		if strings.Contains(line, "names the storage layout") {
			return true
		}
	}
	return false
}

// The real tree obeys the rules.
func TestRepositoryObeysTheRules(t *testing.T) {
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	files := map[string]string{}
	for _, tree := range []string{"cli", "proto/vibesys/cli"} {
		err := filepath.WalkDir(filepath.Join(root, tree), func(p string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return err
			}
			if ext := path.Ext(p); ext != ".go" && ext != ".proto" {
				return nil
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
	}
	if len(files) == 0 {
		t.Fatal("found no sources to check")
	}
	for _, line := range archcheck.Violations(files) {
		t.Error(line)
	}
}

// Product code never imports a package that exists for tests: a fake, a contract suite or a test
// daemon. Each support package is allowed to be imported by test files only, in every package.
func TestSupportPackagesAreImportedByTestsOnly(t *testing.T) {
	support := []string{
		"internal/environment/fakeenv", "internal/supervisor/suptest", "internal/daemon/daemontest",
		"internal/store/storetest", "internal/environment/envtest", "internal/backend/backendtest",
		"internal/environment/fakeenv/fakeworld", "internal/environment/docker/dockerfake",
		"internal/environment/docker/dockertest",
	}
	var productDirs []string
	for dir := range archcheck.Rules() {
		productDirs = append(productDirs, dir)
	}
	for _, target := range support {
		for _, dir := range productDirs {
			if dir == target || slices.Contains(support, dir) || strings.HasSuffix(dir, "test") || strings.HasSuffix(dir, "fakeenv") {
				continue
			}
			got := archcheck.Violations(map[string]string{"cli/" + dir + "/a.go": goFile(module + target)})
			if len(got) == 0 {
				t.Errorf("product code in %s may import the test support package %s", dir, target)
			}
		}
	}
}
