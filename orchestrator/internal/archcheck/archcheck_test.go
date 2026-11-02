package archcheck_test

import (
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
