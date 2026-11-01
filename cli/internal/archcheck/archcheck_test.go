package archcheck_test

import (
	"io/fs"
	"os"
	"path"
	"path/filepath"
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
